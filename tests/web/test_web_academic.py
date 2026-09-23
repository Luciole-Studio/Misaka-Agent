"""学术 URL 路由:纯函数、零 IO,只做 URL → 抓取决策的映射。"""

from __future__ import annotations

import httpx
import pytest

from misaka.core.web.academic import AcademicRoute, route_academic

# (输入 URL, 期望 kind, 期望抓取的 URL)
ROUTES: list[tuple[str, str, str]] = [
    # PMC:规范化到 pmc.ncbi.nlm.nih.gov,老的 www.ncbi 路径也改道过去。
    (
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
        "pmc",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
    ),
    (
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/",
        "pmc",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
    ),
    # 大小写与 query/fragment 都被抹平。
    (
        "http://www.ncbi.nlm.nih.gov/pmc/articles/pmc7654321/?report=classic#abstract",
        "pmc",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC7654321/",
    ),
    # PubMed:原样抓(改道需要 elink 调用,那是 IO,不归这里)。
    (
        "https://pubmed.ncbi.nlm.nih.gov/38123456/",
        "pubmed",
        "https://pubmed.ncbi.nlm.nih.gov/38123456/",
    ),
    # bioRxiv/medRxiv:落地页只有摘要,改道 .full 全文。
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.01.123456v1",
        "biorxiv",
        "https://www.biorxiv.org/content/10.1101/2024.01.01.123456v1.full",
    ),
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.01.123456v1.full.pdf",
        "biorxiv",
        "https://www.biorxiv.org/content/10.1101/2024.01.01.123456v1.full",
    ),
    (
        "https://www.medrxiv.org/content/10.1101/2023.05.05.99999v2.article-info?foo=1",
        "biorxiv",
        "https://www.medrxiv.org/content/10.1101/2023.05.05.99999v2.full",
    ),
    # arXiv:/pdf/ 是二进制,改道摘要页;老式带学科前缀的 id 也要认。
    (
        "https://arxiv.org/pdf/2401.12345v2.pdf",
        "arxiv",
        "https://arxiv.org/abs/2401.12345v2",
    ),
    ("https://arxiv.org/pdf/2401.12345", "arxiv", "https://arxiv.org/abs/2401.12345"),
    (
        "https://arxiv.org/abs/math/0309136",
        "arxiv",
        "https://arxiv.org/abs/math/0309136",
    ),
    # 已经在 HTML 全文上了就别退回摘要页。
    (
        "https://arxiv.org/html/2401.12345v1",
        "arxiv",
        "https://arxiv.org/html/2401.12345v1",
    ),
    # 付费墙:不改 URL,只出警告(改道需要 Unpaywall,见模块说明)。
    (
        "https://www.sciencedirect.com/science/article/pii/S0092867423001234",
        "paywall",
        "https://www.sciencedirect.com/science/article/pii/S0092867423001234",
    ),
    (
        "https://onlinelibrary.wiley.com/doi/10.1002/anie.202012345",
        "paywall",
        "https://onlinelibrary.wiley.com/doi/10.1002/anie.202012345",
    ),
    (
        "https://www.nature.com/articles/s41586-024-01234-5",
        "paywall",
        "https://www.nature.com/articles/s41586-024-01234-5",
    ),
    # FA 清单里逐条列出的 Wiley 子域,靠后缀匹配一并覆盖。
    (
        "https://nph.onlinelibrary.wiley.com/doi/full/10.1111/nph.12345",
        "paywall",
        "https://nph.onlinelibrary.wiley.com/doi/full/10.1111/nph.12345",
    ),
    (
        "https://journals.sagepub.com/doi/10.1177/00030651231234",
        "paywall",
        "https://journals.sagepub.com/doi/10.1177/00030651231234",
    ),
    # 普通 URL 原样通过。
    ("https://example.com/blog/post", "none", "https://example.com/blog/post"),
    ("https://en.wikipedia.org/wiki/DOI", "none", "https://en.wikipedia.org/wiki/DOI"),
    # doi.org 要跳一次才知道落在谁家,这一层不猜;调用方拿到最终 URL 后可以再路由一次。
    ("https://doi.org/10.1038/s41586-024-01234-5", "none", "https://doi.org/10.1038/s41586-024-01234-5"),
    # 认得域名但路径不是文章:不硬凑,原样通过。
    ("https://www.biorxiv.org/", "none", "https://www.biorxiv.org/"),
    ("https://arxiv.org/list/cs.AI/recent", "none", "https://arxiv.org/list/cs.AI/recent"),
    (
        "https://pubmed.ncbi.nlm.nih.gov/?term=crispr",
        "none",
        "https://pubmed.ncbi.nlm.nih.gov/?term=crispr",
    ),
    (
        "https://www.ncbi.nlm.nih.gov/pmc/",
        "none",
        "https://www.ncbi.nlm.nih.gov/pmc/",
    ),
    # 百分号编码解出来的 `?` 不许被拼回构造出的 URL 里。
    (
        "https://arxiv.org/abs/2401.12345%3Fevil=1",
        "none",
        "https://arxiv.org/abs/2401.12345%3Fevil=1",
    ),
    # bioRxiv 这一路是把路径拼回 URL 的,编码必须原样带过去:解码一次再拼回去,
    # `%3F` 会让 httpx 抛 InvalidURL,`%2F` 会静悄悄变成一层新目录。
    (
        "https://www.biorxiv.org/content/10.1101/2024.01.01.1%3Fevil=1",
        "biorxiv",
        "https://www.biorxiv.org/content/10.1101/2024.01.01.1%3Fevil=1.full",
    ),
    (
        "https://www.biorxiv.org/content/10.1101%2F2024.01.01.1",
        "biorxiv",
        "https://www.biorxiv.org/content/10.1101%2F2024.01.01.1.full",
    ),
    # 编码过的 `..` 能活着穿过 httpx 的解析,但 `.path` 会把它解回来:拼回去再让
    # httpx 归一化,改道就指到 /abs/ 之外去了,说明里却还写着「这是本文摘要页」。
    (
        "https://arxiv.org/pdf/x/%2E%2E/%2E%2E/evil",
        "none",
        "https://arxiv.org/pdf/x/%2E%2E/%2E%2E/evil",
    ),
    # 预印本这一路走的是未解码的 raw_path,所以编码原样保留,不会被归一化掉。
    (
        "https://www.biorxiv.org/content/10.1101/x/%2E%2E/y",
        "biorxiv",
        "https://www.biorxiv.org/content/10.1101/x/%2E%2E/y.full",
    ),
    # scheme 白名单:域名认得也不行,非 http(s) 一律不碰。
    ("ftp://arxiv.org/abs/2401.12345", "none", "ftp://arxiv.org/abs/2401.12345"),
    # 域名匹配是「等于或是其子域」,不是「后缀是这串字符」。
    ("https://evilarxiv.org/abs/2401.12345", "none", "https://evilarxiv.org/abs/2401.12345"),
    ("https://notnature.com/articles/x", "none", "https://notnature.com/articles/x"),
    (
        "https://sciencedirect.com.attacker.example/science/article/pii/S1",
        "none",
        "https://sciencedirect.com.attacker.example/science/article/pii/S1",
    ),
    # 大小写与末尾的根点不能让认识的站漏网。
    (
        "https://ARXIV.ORG./pdf/2401.12345",
        "arxiv",
        "https://arxiv.org./abs/2401.12345",
    ),
    # PMC id 的位数有上限:重定向落地 URL 是攻击者挑的,不许拿它灌上下文。
    (
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC" + "1" * 40 + "/",
        "none",
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC" + "1" * 40 + "/",
    ),
    # 超过合法 DNS 名长度的主机名同理——它会被原样写进给模型看的那句话里。
    (
        "https://" + ("a" * 60 + ".") * 5 + "nature.com/articles/x",
        "none",
        "https://" + ("a" * 60 + ".") * 5 + "nature.com/articles/x",
    ),
]


@pytest.mark.parametrize(("url", "kind", "target"), ROUTES)
def test_路由表(url: str, kind: str, target: str) -> None:
    route = route_academic(url)
    assert isinstance(route, AcademicRoute)
    assert route.kind == kind
    assert route.url == target


@pytest.mark.parametrize(("url", "kind", "target"), ROUTES)
def test_只有认出来的站才带说明(url: str, kind: str, target: str) -> None:
    """kind 为 none 时不许往模型上下文里塞话;认出来了就必须给一句可操作的建议。"""
    note = route_academic(url).note
    assert bool(note) is (kind != "none")


@pytest.mark.parametrize(("url", "kind", "target"), ROUTES)
def test_路由幂等(url: str, kind: str, target: str) -> None:
    """把路由结果再喂一次不许漂移——web_fetch 会在重定向落地后二次路由。"""
    assert route_academic(target).url == target


MALFORMED = [
    "",
    "   ",
    "not a url",
    "http://[bad",
    "://missing-scheme",
    "ftp://ftp.example.com/paper.pdf",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "https://",
    "http://arxiv.org",  # 认得的域名但完全没有路径
    "https://arxiv.org/pdf/",  # 有段落没有 id
    "https://arxiv.org/abs/" + "a" * 5000,
    "https://www.biorxiv.org/content/",  # /content/ 之后什么都没有
    "https://pmc.ncbi.nlm.nih.gov/articles/",
    "https://例え.jp/paper",
]


@pytest.mark.parametrize("url", MALFORMED)
def test_畸形_URL_不崩且原样通过(url: str) -> None:
    route = route_academic(url)
    assert route.url == url
    assert route.kind == "none"
    assert route.note == ""


def test_付费墙说明里带上_DOI() -> None:
    """URL 里能挖出 DOI 就报给模型——那是它去找开放获取副本的抓手。"""
    note = route_academic("https://onlinelibrary.wiley.com/doi/10.1002/anie.202012345").note
    assert "10.1002/anie.202012345" in note


def test_付费墙没有_DOI_时不编() -> None:
    note = route_academic(
        "https://www.sciencedirect.com/science/article/pii/S0092867423001234"
    ).note
    assert "DOI" not in note


def test_PMC_说明给出_BioC_兜底端点() -> None:
    """文章页被反爬挡住时,模型要知道还有一个机器可读的入口。"""
    note = route_academic("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/").note
    assert "BioC_json/PMC1234567/unicode" in note


def test_arXiv_摘要页说明里指向_HTML_全文() -> None:
    note = route_academic("https://arxiv.org/pdf/2401.12345v2.pdf").note
    assert "https://arxiv.org/html/2401.12345v2" in note


def test_arXiv_HTML_页说明里指回摘要页() -> None:
    note = route_academic("https://arxiv.org/html/2401.12345v1").note
    assert "https://arxiv.org/abs/2401.12345v1" in note


def test_路由结果不可变() -> None:
    """AcademicRoute 会被传给 web_fetch 再进日志,不许中途被改。"""
    route = route_academic("https://arxiv.org/abs/2401.12345")
    with pytest.raises(AttributeError):
        route.url = "https://evil.example.com/"  # type: ignore[misc]


def test_改道换主机时不许把凭据一起带过去() -> None:
    """PMC 是唯一会换主机的改写,`user:pass@` 不许跟着走到新主机上。

    (vet_public_url 本来就会拒收带凭据的 URL;这里钉的是改写层自己不制造跨源转发。)
    """
    route = route_academic("https://user:pass@www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/")
    assert route.kind == "pmc"
    assert route.url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/"
    assert "user" not in route.url
    assert "pass" not in route.url


def test_DOI_进说明前先截断() -> None:
    """DOI 后缀没有长度上限,而这段话是直接进模型上下文的。"""
    note = route_academic("https://www.sciencedirect.com/10.1234/" + "a" * 300).note
    assert "10.1234/" + "a" * 192 in note
    assert "a" * 193 not in note


def test_改写失败退化为不改道而不是抛出() -> None:
    """web_fetch 会拿重定向落地的 URL 再路由一次;那一步抛异常就变成工具报错。"""
    def 炸(*args: object, **kwargs: object) -> httpx.URL:
        raise httpx.InvalidURL("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.URL, "copy_with", 炸)
        assert route_academic("https://arxiv.org/pdf/2401.12345") == AcademicRoute(
            "https://arxiv.org/pdf/2401.12345", "none", ""
        )
