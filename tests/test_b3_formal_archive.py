import hashlib

from tools.verify_b3_formal_archive import verify_inventory


def test_verify_inventory_detects_match_and_tamper(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    payload = root / "verdicts.csv"
    payload.write_bytes(b"gate,pass\nstability,false\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    inventory = {
        "core_files": [
            {
                "path": "verdicts.csv",
                "size": payload.stat().st_size,
                "sha256": digest,
            }
        ]
    }
    assert verify_inventory(root, inventory) == []
    payload.write_bytes(b"gate,pass\nstability,true \n")
    assert verify_inventory(root, inventory) == ["verdicts.csv: sha256 mismatch"]
