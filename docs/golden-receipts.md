# Pulp golden receipts

A promoted golden carries a sidecar receipt next to the image. The receipt is
validated by `providers/common/pulp-golden-receipt.py` and follows the JSON
schema beside it. It records the toolchain manifest digest, Pulp source commit,
provider digests, image digest, UTC bake time, and versions observed inside the
guest.

`./tartci goldens doctor` is read-only. It reports stale or mismatched receipts
without changing a host, image, runner, or lane. Add `--promotion` (or
`--release`) only to the explicit promotion or release preflight; then a stale
or mismatched receipt exits non-zero. A running lane is never stopped and a
host is never auto-fixed because a receipt is stale.

The freshness windows are 14 days for gate and release images and 30 days for
development images. `latest` is only a pointer. The receipt and image digest
are the identity that a promotion reviews.
