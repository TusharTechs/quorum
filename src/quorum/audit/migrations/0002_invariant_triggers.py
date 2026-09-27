"""Invariants enforced by PostgreSQL itself, so they hold even for code paths that bypass
the application (Django admin, a manage.py shell, a psql session with the app role).

1. audit log is append-only
2. ranking runs are immutable
3. review scores stay inside their criterion's scale
4. submission content cannot change after the deadline (+grace); no new submissions either
5. a judge cannot be a member of a team in an event they judge (and vice versa)
6. team size cap holds under concurrency (row lock on the team)
7. a criterion's key/weight/scale are frozen once any score uses it
8. a locked judging method is immutable (changes create a new, audited version)

Escape hatch: `SET LOCAL quorum.bypass = 'on'` inside a transaction (used only by the
fixture importer and the audited admin override), never by request handlers.
"""

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION quorum_bypass() RETURNS boolean AS $$
  SELECT coalesce(current_setting('quorum.bypass', true), '') = 'on'
$$ LANGUAGE sql STABLE;

-- 1. audit log append-only
CREATE OR REPLACE FUNCTION quorum_audit_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit log is append-only (% on seq %)', TG_OP, OLD.seq USING ERRCODE = 'P0001';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_append_only ON audit_auditevent;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_auditevent
  FOR EACH ROW EXECUTE FUNCTION quorum_audit_append_only();

-- 2. ranking runs immutable
CREATE OR REPLACE FUNCTION quorum_run_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'ranking runs are immutable' USING ERRCODE = 'P0001';
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS run_immutable ON results_rankingrun;
CREATE TRIGGER run_immutable BEFORE UPDATE OR DELETE ON results_rankingrun
  FOR EACH ROW EXECUTE FUNCTION quorum_run_immutable();

-- 3. score within scale
CREATE OR REPLACE FUNCTION quorum_score_in_scale() RETURNS trigger AS $$
DECLARE lo int; hi int;
BEGIN
  SELECT scale_min, scale_max INTO lo, hi FROM judging_criterion WHERE id = NEW.criterion_id;
  IF NEW.value < lo OR NEW.value > hi THEN
    RAISE EXCEPTION 'score % outside criterion scale %..%', NEW.value, lo, hi USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS score_in_scale ON judging_reviewscore;
CREATE TRIGGER score_in_scale BEFORE INSERT OR UPDATE ON judging_reviewscore
  FOR EACH ROW EXECUTE FUNCTION quorum_score_in_scale();

-- 4. deadline
CREATE OR REPLACE FUNCTION quorum_project_deadline() RETURNS trigger AS $$
DECLARE closes timestamptz;
BEGIN
  IF quorum_bypass() THEN RETURN NEW; END IF;
  SELECT submissions_close_at + make_interval(secs => grace_seconds) INTO closes
    FROM events_event WHERE id = NEW.event_id;
  IF now() < closes THEN RETURN NEW; END IF;
  IF TG_OP = 'INSERT' THEN
    RAISE EXCEPTION 'deadline_passed: submissions closed at %', closes USING ERRCODE = 'P0001';
  END IF;
  IF (NEW.title, NEW.tagline, NEW.description_md, NEW.thumbnail, NEW.demo_video_url, NEW.repo_url,
      NEW.live_url, NEW.declared_commit_sha, NEW.tech_tags, NEW.track_id, NEW.team_id)
     IS DISTINCT FROM
     (OLD.title, OLD.tagline, OLD.description_md, OLD.thumbnail, OLD.demo_video_url, OLD.repo_url,
      OLD.live_url, OLD.declared_commit_sha, OLD.tech_tags, OLD.track_id, OLD.team_id)
     OR (OLD.status = 'draft' AND NEW.status = 'submitted') THEN
    RAISE EXCEPTION 'deadline_passed: submission content is frozen since %', closes USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS project_deadline ON events_project;
CREATE TRIGGER project_deadline BEFORE INSERT OR UPDATE ON events_project
  FOR EACH ROW EXECUTE FUNCTION quorum_project_deadline();

-- 5a. a team member cannot be a judge of the same event
CREATE OR REPLACE FUNCTION quorum_member_not_judge() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM events_eventrole WHERE event_id = NEW.event_id AND user_id = NEW.user_id AND role = 'judge') THEN
    RAISE EXCEPTION 'a judge cannot join a team in an event they judge' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS member_not_judge ON events_teammember;
CREATE TRIGGER member_not_judge BEFORE INSERT ON events_teammember
  FOR EACH ROW EXECUTE FUNCTION quorum_member_not_judge();

-- 5b. ... and a team member cannot be made a judge of that event
CREATE OR REPLACE FUNCTION quorum_judge_not_member() RETURNS trigger AS $$
BEGIN
  IF NEW.role = 'judge' AND EXISTS (SELECT 1 FROM events_teammember WHERE event_id = NEW.event_id AND user_id = NEW.user_id) THEN
    RAISE EXCEPTION 'a team member cannot judge the same event' USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS judge_not_member ON events_eventrole;
CREATE TRIGGER judge_not_member BEFORE INSERT OR UPDATE ON events_eventrole
  FOR EACH ROW EXECUTE FUNCTION quorum_judge_not_member();

-- 6. team size cap under concurrency
CREATE OR REPLACE FUNCTION quorum_team_size() RETURNS trigger AS $$
DECLARE cap int; n int;
BEGIN
  PERFORM 1 FROM events_team WHERE id = NEW.team_id FOR UPDATE;
  SELECT team_size_max INTO cap FROM events_event WHERE id = NEW.event_id;
  SELECT count(*) INTO n FROM events_teammember WHERE team_id = NEW.team_id;
  IF n >= cap AND NOT quorum_bypass() THEN
    RAISE EXCEPTION 'team is full (% members max)', cap USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS team_size ON events_teammember;
CREATE TRIGGER team_size BEFORE INSERT ON events_teammember
  FOR EACH ROW EXECUTE FUNCTION quorum_team_size();

-- 7. criteria frozen once scored
CREATE OR REPLACE FUNCTION quorum_criterion_frozen() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM judging_reviewscore WHERE criterion_id = OLD.id) AND NOT quorum_bypass() THEN
    IF TG_OP = 'DELETE' OR (NEW.key, NEW.weight_bp, NEW.scale_min, NEW.scale_max)
                           IS DISTINCT FROM (OLD.key, OLD.weight_bp, OLD.scale_min, OLD.scale_max) THEN
      RAISE EXCEPTION 'criterion % is already used by scores; its key, weight and scale are frozen', OLD.key
        USING ERRCODE = 'P0001';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS criterion_frozen ON judging_criterion;
CREATE TRIGGER criterion_frozen BEFORE UPDATE OR DELETE ON judging_criterion
  FOR EACH ROW EXECUTE FUNCTION quorum_criterion_frozen();

-- 8. locked method immutable
CREATE OR REPLACE FUNCTION quorum_method_locked() RETURNS trigger AS $$
BEGIN
  IF OLD.locked_at IS NOT NULL AND NOT quorum_bypass() THEN
    RAISE EXCEPTION 'judging method v% is locked; create a new version via an audited override', OLD.version
      USING ERRCODE = 'P0001';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS method_locked ON judging_judgingmethod;
CREATE TRIGGER method_locked BEFORE UPDATE OR DELETE ON judging_judgingmethod
  FOR EACH ROW EXECUTE FUNCTION quorum_method_locked();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS audit_append_only ON audit_auditevent;
DROP TRIGGER IF EXISTS run_immutable ON results_rankingrun;
DROP TRIGGER IF EXISTS score_in_scale ON judging_reviewscore;
DROP TRIGGER IF EXISTS project_deadline ON events_project;
DROP TRIGGER IF EXISTS member_not_judge ON events_teammember;
DROP TRIGGER IF EXISTS judge_not_member ON events_eventrole;
DROP TRIGGER IF EXISTS team_size ON events_teammember;
DROP TRIGGER IF EXISTS criterion_frozen ON judging_criterion;
DROP TRIGGER IF EXISTS method_locked ON judging_judgingmethod;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("events", "0001_initial"),
        ("judging", "0002_initial"),
        ("results", "0001_initial"),
    ]
    operations = [migrations.RunSQL(SQL, REVERSE)]
