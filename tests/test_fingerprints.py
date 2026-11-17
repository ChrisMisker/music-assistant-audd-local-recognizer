from audd_local_recognizer.fingerprints import density_subset, score_reference

def test_density_is_deterministic():
    hashes = {f"{i:016x}": [i] for i in range(256)}
    assert density_subset(hashes) == density_subset(hashes)
    assert 8 <= len(density_subset(hashes)) <= 24

def test_offset_votes():
    query = {"aa": [10], "bb": [20], "cc": [30]}
    stored = [("aa", 15), ("bb", 25), ("cc", 35)]
    shared, offset, votes, ratio = score_reference(query, stored)
    assert (shared, offset, votes, ratio) == (3, 5, 3, 1.0)
