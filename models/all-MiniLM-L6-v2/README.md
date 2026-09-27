# all-MiniLM-L6-v2 (quantized ONNX)

Sentence embeddings for Quorum Intelligence: search, possible duplicates, the feedback
coach and "Ask Quorum" intent matching. Runs on CPU inside the stack, with no network access.

- Model: [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2),
  licence **Apache-2.0** (see `MODEL_CARD.md`).
- Files: the int8-quantized ONNX export and tokenizer from
  [Xenova/all-MiniLM-L6-v2](https://huggingface.co/Xenova/all-MiniLM-L6-v2), 384-dimensional mean-pooled output.
- Integrity (checked at load by `quorum.intelligence.embed`):

| File | SHA-256 |
|---|---|
| `model_quantized.onnx` | `afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1` |
| `tokenizer.json` (stored as `tokenizer.json.gz`; the hash is of the decompressed upstream file) | `da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0` |
