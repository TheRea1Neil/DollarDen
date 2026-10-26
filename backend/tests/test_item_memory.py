"""Receipt-line memory: ask once, then remember.

The case that motivates all of this: a Walmart slip prints COTTAGE CH,
GV MXDVG 12Z, HF SMK SGE. The model filed the legible lines and
declined the rest — correct for a guess, and useless as a recurring
experience, because the same questions come back every shop.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient

from app.budget import item_memory
from app.budget.item_memory import slug
from app.db.models import Category, User
from app.db.session import SessionLocal
from tests.conftest import requires_db

PW = "item-memory-passphrase-99"


def _auth(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/register",
                    json={"email": f"im-{uuid.uuid4().hex[:10]}@x.com", "password": PW})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _cat(client, h, name) -> dict:
    r = client.post("/api/budget/categories",
                    json={"name": name, "kind": "spending"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


class TestSlug:
    def test_strips_the_product_code_and_tax_flag(self):
        assert slug("COTTAGE CH  015700251020 F  2.88 R") == "cottage ch"

    def test_the_same_item_with_a_different_barcode_matches(self):
        assert slug("HF SMK SGE 044500341710 F") == slug("HF SMK SGE 099900112233 F")

    def test_pack_size_is_ignored(self):
        """Same product, different tin — not worth teaching twice."""
        assert slug("GV BFLOR 12") == slug("GV BFLOR 16")

    def test_case_and_punctuation(self):
        assert slug("Mango Chunk!") == slug("MANGO CHUNK")

    def test_a_bare_price_teaches_nothing(self):
        assert slug("4.24 R") == ""


@requires_db
class TestRememberAndRecall:
    def _setup(self, client):
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        return h, groceries

    def test_a_line_is_asked_once_then_known(self, client: TestClient):
        h, groceries = self._setup(client)

        before = client.post("/api/budget/item-memory/recall",
                             json={"merchant": "Walmart",
                                   "descriptions": ["COTTAGE CH"]}, headers=h)
        assert before.json()["recollections"] == {}

        r = client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "WALMART SUPERCENTER #1234",
            "items": [{"description": "COTTAGE CH",
                       "category_id": groceries["id"]}]})
        assert r.status_code == 200 and r.json()["remembered"] == 1

        after = client.post("/api/budget/item-memory/recall",
                            json={"merchant": "Walmart #9999",
                                  "descriptions": ["COTTAGE CH"]}, headers=h)
        rec = after.json()["recollections"]["COTTAGE CH"]
        assert rec["category_id"] == groceries["id"]
        assert rec["category_name"] == "Food & Groceries"
        # "walmart supercenter" vs "walmart": a whole-word prefix, so
        # the same shop rather than a borrowed cross-shop memory.
        assert rec["source"] == "merchant"

    def test_a_different_barcode_still_matches(self, client: TestClient):
        h, groceries = self._setup(client)
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "COTTAGE CH 015700251020 F 2.88 R",
                       "category_id": groceries["id"]}]})
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["COTTAGE CH 099900112233 F 3.10 R"]},
                          headers=h).json()
        assert out["recollections"], "the product code must not defeat the match"

    def test_seeing_it_again_reinforces(self, client: TestClient):
        h, groceries = self._setup(client)
        for _ in range(3):
            client.post("/api/budget/item-memory", headers=h, json={
                "merchant": "Walmart",
                "items": [{"description": "COTTAGE CH",
                           "category_id": groceries["id"]}]})
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["COTTAGE CH"]}, headers=h).json()
        assert out["recollections"]["COTTAGE CH"]["times_seen"] == 3

    def test_a_correction_overwrites_rather_than_accumulating(self, client: TestClient):
        """The last thing chosen is what was meant."""
        h, groceries = self._setup(client)
        household = _cat(client, h, "Household")
        for cat in (groceries, household):
            client.post("/api/budget/item-memory", headers=h, json={
                "merchant": "Walmart",
                "items": [{"description": "PAPER TOWEL", "category_id": cat["id"]}]})
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["PAPER TOWEL"]}, headers=h).json()
        rec = out["recollections"]["PAPER TOWEL"]
        assert rec["category_id"] == household["id"]
        assert rec["times_seen"] == 1, "a changed answer restarts the count"


@requires_db
class TestScope:
    def test_another_shop_does_not_see_this_shops_code(self, client: TestClient):
        """GV is Walmart's house brand and means nothing elsewhere."""
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "GV MXDVG 12Z", "category_id": groceries["id"]}]})

        same = client.post("/api/budget/item-memory/recall",
                           json={"merchant": "Walmart",
                                 "descriptions": ["GV MXDVG 12Z"]}, headers=h).json()
        assert same["recollections"]["GV MXDVG 12Z"]["source"] == "merchant"

        other = client.post("/api/budget/item-memory/recall",
                            json={"merchant": "Trader Joe's",
                                  "descriptions": ["GV MXDVG 12Z"]}, headers=h).json()
        # One shop knows it, and nothing contradicts it, so it carries.
        assert other["recollections"]["GV MXDVG 12Z"]["source"] == "shared"

    def test_shops_that_disagree_fall_back_to_asking(self, client: TestClient):
        """Ambiguity goes to the human, not to a guess."""
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        household = _cat(client, h, "Household")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "SPONGE", "category_id": groceries["id"]}]})
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Target",
            "items": [{"description": "SPONGE", "category_id": household["id"]}]})

        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Kroger",
                                "descriptions": ["SPONGE"]}, headers=h).json()
        assert out["recollections"] == {}, "disagreement must not be guessed"

    def test_a_chains_store_formats_still_find_the_memory(self, client: TestClient):
        """Different formats of one chain print the same product codes.

        "WALMART SUPERCENTER #1234" and "Walmart Neighborhood Market"
        normalise to "walmart supercenter" and "walmart neighborhood
        market" — neither a prefix of the other, sharing only a first
        word. They are reached by the cross-shop path rather than the
        same-shop one, and that is the right outcome: the memory is
        still offered, labelled "shared" because it honestly was
        learned somewhere else.

        Deliberately NOT fixed with a first-word "same chain" rule.
        That would make "Trader Joe's" and "Trader Vic's" one shop, and
        inventing chain heuristics is exactly the over-fitting that had
        the receipt filter deleting real purchases.
        """
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "WALMART SUPERCENTER #1234",
            "items": [{"description": "GV MXDVG 12Z",
                       "category_id": groceries["id"]}]})
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart Neighborhood Market",
                                "descriptions": ["GV MXDVG 12Z"]}, headers=h).json()
        rec = out["recollections"]["GV MXDVG 12Z"]
        assert rec["category_id"] == groceries["id"]
        assert rec["source"] == "shared"

    def test_a_store_number_does_not_split_a_memory(self, client: TestClient):
        """"Walmart #1234" and "Walmart #9999" are one shop."""
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart #1234",
            "items": [{"description": "COTTAGE CH",
                       "category_id": groceries["id"]}]})
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart #9999",
                                "descriptions": ["COTTAGE CH"]}, headers=h).json()
        assert out["recollections"]["COTTAGE CH"]["source"] == "merchant"

    def test_a_prefix_match_needs_a_word_boundary(self, client: TestClient):
        """"target" must not match "targeted goods"."""
        from app.budget.item_memory import _same_shop
        assert _same_shop("walmart", "walmart supercenter")
        assert _same_shop("walmart supercenter", "walmart")
        assert not _same_shop("target", "targeted goods")
        assert not _same_shop("", "walmart")

    def test_the_shops_own_memory_beats_a_shared_one(self, client: TestClient):
        h = _auth(client)
        groceries = _cat(client, h, "Food & Groceries")
        household = _cat(client, h, "Household")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Target",
            "items": [{"description": "SPONGE", "category_id": household["id"]}]})
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "SPONGE", "category_id": groceries["id"]}]})

        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["SPONGE"]}, headers=h).json()
        rec = out["recollections"]["SPONGE"]
        assert rec["category_id"] == groceries["id"] and rec["source"] == "merchant"


@requires_db
class TestSafety:
    def test_a_memory_for_an_archived_category_is_not_offered(self, client: TestClient):
        """It would pre-fill something that cannot be submitted."""
        h = _auth(client)
        cat = _cat(client, h, "Temp")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "WIDGET", "category_id": cat["id"]}]})
        assert client.delete(f"/api/budget/categories/{cat['id']}",
                             headers=h).status_code in (200, 204)

        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["WIDGET"]}, headers=h).json()
        assert out["recollections"] == {}

    def test_another_users_memory_is_invisible(self, client: TestClient):
        a = _auth(client)
        cat = _cat(client, a, "Food")
        client.post("/api/budget/item-memory", headers=a, json={
            "merchant": "Walmart",
            "items": [{"description": "COTTAGE CH", "category_id": cat["id"]}]})

        b = _auth(client)
        out = client.post("/api/budget/item-memory/recall",
                          json={"merchant": "Walmart",
                                "descriptions": ["COTTAGE CH"]}, headers=b).json()
        assert out["recollections"] == {}

    def test_a_category_belonging_to_someone_else_is_refused(self, client: TestClient):
        a, b = _auth(client), _auth(client)
        theirs = _cat(client, a, "Food")
        r = client.post("/api/budget/item-memory", headers=b, json={
            "merchant": "Walmart",
            "items": [{"description": "X", "category_id": theirs["id"]}]})
        assert r.status_code == 400

    def test_forgetting(self, client: TestClient):
        h = _auth(client)
        cat = _cat(client, h, "Food")
        client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "COTTAGE CH", "category_id": cat["id"]}]})
        listed = client.get("/api/budget/item-memory", headers=h).json()["items"]
        assert len(listed) == 1
        assert client.delete(f"/api/budget/item-memory/{listed[0]['id']}",
                             headers=h).status_code == 204
        assert client.get("/api/budget/item-memory", headers=h).json()["items"] == []

    def test_a_price_only_line_teaches_nothing(self, client: TestClient):
        h = _auth(client)
        cat = _cat(client, h, "Food")
        r = client.post("/api/budget/item-memory", headers=h, json={
            "merchant": "Walmart",
            "items": [{"description": "4.24 R", "category_id": cat["id"]}]})
        assert r.json()["remembered"] == 0
