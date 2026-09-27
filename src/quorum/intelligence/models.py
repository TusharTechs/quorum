from django.db import models


class Embedding(models.Model):
    """Cached sentence embedding of an object's text, keyed by a hash of (model, text):
    an edit changes the hash, so a stale vector is never used."""

    kind = models.CharField(max_length=24)          # "project"
    object_id = models.CharField(max_length=64)
    content_hash = models.CharField(max_length=64)
    vector = models.BinaryField()                   # float32 x 384, L2-normalised
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "object_id"], name="uniq_embedding_object")]
        indexes = [models.Index(fields=["kind", "content_hash"])]
