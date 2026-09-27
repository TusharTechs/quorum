"""Who is asking. Roles are per event (a judge at one event can be a participant at the
next); admin is global. Everything is computed from the database on each request."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Forbidden, NotAuthenticated

ORGANIZER, JUDGE, PARTICIPANT = "organizer", "judge", "participant"


@dataclass
class Actor:
    user: object | None
    via: str | None = None  # "session" | "token" | None
    _roles: dict = field(default_factory=dict)

    @classmethod
    def of(cls, request) -> Actor:
        cached = getattr(request, "_quorum_actor", None)
        if cached is not None:
            return cached
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            a = cls(user=None)
        else:
            a = cls(user=user, via=getattr(request, "auth_via", "session"))
        request._quorum_actor = a
        return a

    @property
    def authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_admin(self) -> bool:
        return bool(self.user and self.user.is_superuser)

    def roles(self, event) -> set[str]:
        if not self.user or event is None:
            return set()
        if event.pk not in self._roles:
            from quorum.events.models import EventRole

            self._roles[event.pk] = set(
                EventRole.objects.filter(event=event, user=self.user).values_list("role", flat=True)
            )
        return self._roles[event.pk]

    def has(self, event, *roles) -> bool:
        return bool(self.roles(event) & set(roles))

    def is_organizer(self, event) -> bool:
        return self.is_admin or ORGANIZER in self.roles(event)

    def judge_role(self, event):
        if not self.user:
            return None
        from quorum.events.models import EventRole

        return EventRole.objects.filter(event=event, user=self.user, role=JUDGE).first()

    # --- guards -------------------------------------------------------------
    def require_auth(self):
        if not self.authenticated:
            raise NotAuthenticated("Sign in or send an API token.")
        return self

    def require_admin(self):
        self.require_auth()
        if not self.is_admin:
            raise Forbidden("Administrators only.")
        return self

    def require_organizer(self, event):
        self.require_auth()
        if not self.is_organizer(event):
            raise Forbidden("Organizers of this event only.")
        return self

    def require_judge(self, event):
        self.require_auth()
        jr = self.judge_role(event)
        if jr is None:
            raise Forbidden("Judges of this event only.")
        return jr

    def require_participant(self, event):
        self.require_auth()
        if PARTICIPANT not in self.roles(event) and not self.is_admin:
            raise Forbidden("Participants of this event only.")
        return self
