import os, tempfile, platform, sys
from pathlib import Path
print(platform.mac_ver(), sys.version)
def trial(name, mode, resolved_src, resolved_dst):
    td = Path(tempfile.mkdtemp())
    gen = td / "generations"; gen.mkdir()
    sroot = gen.resolve() if resolved_src else gen
    droot = gen.resolve() if resolved_dst else gen
    staged = Path(tempfile.mkdtemp(prefix=".x.", dir=sroot))
    (staged / "f").write_text("x")
    staged.chmod(mode)
    try:
        os.rename(staged, droot / "final")
        print(f"{name}: ok  src={staged} dst={droot/'final'}")
    except OSError as e:
        print(f"{name}: {type(e).__name__} {e.errno}")
    finally:
        for p in (staged, droot / "final"):
            if p.exists(): p.chmod(0o755)
for mode in (0o555, 0o755):
    for rs, rd in ((False, True), (True, True), (False, False)):
        trial(f"mode={oct(mode)} src_resolved={rs} dst_resolved={rd}", mode, rs, rd)
