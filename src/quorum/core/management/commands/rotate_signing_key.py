from django.core.management.base import BaseCommand
from django.db import transaction

from quorum.audit import service as audit
from quorum.core.crypto import rotate_key


class Command(BaseCommand):
    help = ("Retire the Ed25519 signing key and start a new one. Records signed before the rotation "
            "keep verifying (the old public key stays published, marked retired); the old private key "
            "is deleted. Back up QUORUM_DATA_DIR/keys first if your policy requires it.")

    def handle(self, *args, **o):
        with transaction.atomic():
            old, new = rotate_key()
            audit.record("SIGNING_KEY_ROTATED", f"Signing key rotated: {old or '(none)'} retired, {new} active",
                         actor_role="system", data={"retired": old, "active": new})
        self.stdout.write(f"retired {old or '(none)'}; new signing key {new}")
        self.stdout.write("published: /.well-known/quorum-keys.json (the retired key stays, marked retired)")
