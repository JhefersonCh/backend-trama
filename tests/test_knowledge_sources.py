from tests.test_knowledge_api import kc, upload  # noqa: F401  (fixture re-use)

RFC = (
    b"Network Working Group                                          S. Cheshire\n"
    b"Request for Comments: 3927                                Apple Computer\n"
    b"                                                                May 2005\n\n"
    b"Abstract\n\n   Link-local addresses for hosts without a DHCP server.\n\n"
    b"Cheshire, et al.            Standards Track                     [Page 1]\f\n"
    b"RFC 3927                    IPv4 Link-Local                     May 2005\n\n"
    b"1.  Introduction\n\n   Hosts MAY pick an address in 169.254/16 when no DHCP\n   server answers.\n\n"
    b"1.1.  Requirements\n\n   The key words MUST and SHOULD are used as in RFC 2119.\n"
)


async def test_rfc_text_is_cleaned_and_split_by_section(kc):  # noqa: F811
    r = await upload(kc, RFC, "rfc3927.txt", document_type="standard", language="en")
    assert r.status_code == 201, r.text
    doc_id = r.json()["document"]["id"]
    preview = (await kc.get(f"/api/knowledge/documents/{doc_id}")).json()["preview"]
    text = "\n".join(c["content"] for c in preview)
    assert "[Page" not in text and "\f" not in text and "IPv4 Link-Local" not in text
    assert {c["metadata"]["section"] for c in preview} >= {"1. Introduction", "1. Introduction > 1.1. Requirements"}
    assert "Hosts MAY pick an address in 169.254/16 when no DHCP server answers." in text  # unwrapped
