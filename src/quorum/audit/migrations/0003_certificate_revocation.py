"""Certificate revocation, and invariant 10: a signed record never changes.

The payload, signature, key and identity of a certificate are frozen once issued; the only
change allowed is revocation, and revocation is final (it cannot be cleared or reworded).
Deleting the row stays possible because erasing a person deletes their user row.
"""

from django.db import migrations, models

SQL = r"""
CREATE OR REPLACE FUNCTION quorum_certificate_frozen() RETURNS trigger AS $$
BEGIN
  IF quorum_bypass() THEN RETURN NEW; END IF;
  IF NEW.payload <> OLD.payload OR NEW.signature <> OLD.signature OR NEW.key_id <> OLD.key_id
     OR NEW.serial <> OLD.serial OR NEW.kind <> OLD.kind OR NEW.event_id <> OLD.event_id
     OR NEW.user_id <> OLD.user_id OR NEW.issued_at <> OLD.issued_at THEN
    RAISE EXCEPTION 'certificate % is signed and cannot change', OLD.serial USING ERRCODE = 'P0001';
  END IF;
  IF OLD.revoked_at IS NOT NULL AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                                    OR NEW.revoked_reason <> OLD.revoked_reason) THEN
    RAISE EXCEPTION 'certificate % is revoked; revocation is final', OLD.serial USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS certificate_frozen ON audit_certificate;
CREATE TRIGGER certificate_frozen BEFORE UPDATE ON audit_certificate
  FOR EACH ROW EXECUTE FUNCTION quorum_certificate_frozen();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS certificate_frozen ON audit_certificate;
DROP FUNCTION IF EXISTS quorum_certificate_frozen();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0002_invariant_triggers"),
    ]

    operations = [
        migrations.AddField(
            model_name="certificate",
            name="revoked_reason",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.RunSQL(SQL, REVERSE),
    ]
