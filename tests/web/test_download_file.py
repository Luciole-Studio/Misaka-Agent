"""受控下载工具:字节上限、类型校验、文件名消毒、私网拒绝。全程不联网。"""
from __future__ import annotations

import functools
import hashlib
import os
import threading

import httpx
import pytest

from misaka.core.documents import index as corpus
from misaka.core.tools import download_file
from misaka.core.tools.download_file import (
    DownloadFileToolInput,
    create_download_file_tool_definition,
)
from misaka.core.web import bounded

PUBLIC = "93.184.216.34"
PDF = b"%PDF-1.7\n" + b"x" * 200
URL = "https://files.example/paper.pdf"


@pytest.fixture
def serve(monkeypatch):
    """把 DNS 换成固定公网地址,并让工具的每次请求都由给定 handler 应答。

    换掉的是 ``bounded`` 的解析函数而不是整条 ``open_checked_stream``:SSRF 校验、
    逐跳重定向、地址钉扎都还是真代码在跑,测试只是不让它碰真网络。
    """
    default = [PUBLIC]
    overrides: dict[str, str] = {}

    async def _resolve_host(host, _port):
        return [overrides.get(host, default[0])]

    monkeypatch.setattr(bounded, "_resolve_host", _resolve_host)

    def install(handler, *, resolves_to=PUBLIC, hosts=None):
        default[0] = resolves_to
        overrides.clear()
        overrides.update(hosts or {})
        monkeypatch.setattr(
            download_file,
            "open_checked_stream",
            functools.partial(bounded.open_checked_stream, transport=httpx.MockTransport(handler)),
        )

    return install


def _static(body: bytes, *, content_type="application/pdf", headers=None, status=200):
    def handler(_request):
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": content_type, **(headers or {})},
        )

    return handler


class _Signal:
    """最小的中止信号:工具只读 ``aborted``。"""

    def __init__(self):
        self.aborted = False


async def _run(workspace, url=URL, path="", signal=None):
    definition = create_download_file_tool_definition(str(workspace))
    return await definition.execute("call-1", DownloadFileToolInput(url=url, path=path), signal, None, None)


def _text(result):
    return result.content[0].text


def _downloads(workspace):
    directory = workspace / "downloads"
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


async def test_downloads_to_the_workspace_with_a_correct_digest(serve, workspace):
    """正常下载:文件落在 downloads/,details 带 (url, path, sha256, bytes, content_type)。"""
    serve(_static(PDF))

    result = await _run(workspace)

    assert _downloads(workspace) == ["paper.pdf"]
    saved = workspace / "downloads" / "paper.pdf"
    assert saved.read_bytes() == PDF
    assert result.details["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert result.details["bytes"] == len(PDF)
    assert result.details["content_type"] == "application/pdf"
    assert result.details["url"] == URL
    assert result.details["path"] == str(saved)
    # 正文不进上下文,只给路径。
    assert "%PDF" not in _text(result)
    assert str(saved) in _text(result)


async def test_declared_oversize_is_refused_before_any_transfer(serve, workspace):
    """声明的 Content-Length 超限:一个字节都不传,也不建文件。"""
    read = []

    async def body():
        read.append(1)
        yield PDF

    def handler(_request):
        return httpx.Response(
            200,
            content=body(),
            headers={"content-type": "application/pdf", "content-length": str(download_file.MAX_DOWNLOAD_BYTES + 1)},
        )

    serve(handler)

    result = await _run(workspace)

    assert read == []
    assert _downloads(workspace) == []
    assert "declares" in _text(result)


async def test_actual_oversize_aborts_and_deletes_the_partial_file(serve, monkeypatch, workspace):
    """声明缺失时以实际累计字节为准:超限即中止,半成品必须删掉。"""
    monkeypatch.setattr(download_file, "MAX_DOWNLOAD_BYTES", 1024)

    async def body():
        yield b"%PDF-1.7\n"
        for _ in range(4):
            yield b"y" * 512

    def handler(_request):
        # 没有 content-length:分块响应本来就不声明大小,预检无从下手。
        return httpx.Response(200, content=body(), headers={"content-type": "application/pdf"})

    serve(handler)

    result = await _run(workspace)

    assert _downloads(workspace) == []
    assert "larger than" in _text(result)


async def test_type_disguise_is_rejected_and_the_file_deleted(serve, workspace):
    """声明 .pdf 实为付费墙 HTML:magic 不符 → 删文件 + 报错(fail-closed)。"""
    serve(_static(b"<!DOCTYPE html><html><body>Sign in to continue", content_type="text/html"))

    result = await _run(workspace)

    assert _downloads(workspace) == []
    assert "not .pdf content" in _text(result)


async def test_binary_blob_wearing_a_text_extension_is_rejected(serve, workspace):
    """文本类扩展名没有签名可比,靠 NUL 字节判定它其实是二进制。"""
    serve(_static(b"\x00\x01\x02binary", content_type="text/csv"))

    result = await _run(workspace, url="https://files.example/data.csv")

    assert _downloads(workspace) == []
    assert "not .csv content" in _text(result)


async def test_unsupported_type_is_refused(serve, workspace):
    """不在白名单里的类型(可执行文件)直接拒绝。"""
    serve(_static(b"\x7fELF", content_type="application/octet-stream"))

    result = await _run(workspace, url="https://files.example/tool.exe")

    assert _downloads(workspace) == []
    assert "not a downloadable type" in _text(result)


async def test_content_disposition_traversal_is_sanitized(serve, workspace):
    """远端给的 Content-Disposition 想穿越目录,只保留最后一段。"""
    serve(_static(PDF, headers={"content-disposition": 'attachment; filename="../../evil.pdf"'}))

    result = await _run(workspace, url="https://files.example/download")

    assert _downloads(workspace) == ["evil.pdf"]
    assert not (workspace.parent / "evil.pdf").exists()
    assert result.details["path"] == str(workspace / "downloads" / "evil.pdf")


async def test_caller_path_argument_cannot_escape_the_download_directory(serve, workspace):
    """模型自己给的 path 同样只当文件名用,绝对路径的目录部分被丢弃。"""
    serve(_static(PDF))

    result = await _run(workspace, path="/etc/cron.d/evil.pdf")

    assert _downloads(workspace) == ["evil.pdf"]
    assert result.details["path"] == str(workspace / "downloads" / "evil.pdf")


async def test_second_download_of_the_same_name_does_not_overwrite(serve, workspace):
    """同名去重:第二份落成 paper-1.pdf,第一份原样保留。"""
    serve(_static(PDF))
    await _run(workspace)

    other = b"%PDF-1.7\n" + b"z" * 40
    serve(_static(other))
    result = await _run(workspace)

    assert _downloads(workspace) == ["paper-1.pdf", "paper.pdf"]
    assert (workspace / "downloads" / "paper.pdf").read_bytes() == PDF
    assert (workspace / "downloads" / "paper-1.pdf").read_bytes() == other
    assert result.details["sha256"] == hashlib.sha256(other).hexdigest()


async def test_abort_mid_stream_deletes_the_partial_file(serve, workspace):
    """传输途中被中止:抛中止错,半成品不许留在盘上。"""
    signal = _Signal()

    async def body():
        yield b"%PDF-1.7\n"
        signal.aborted = True
        yield b"y" * 64

    def handler(_request):
        return httpx.Response(200, content=body(), headers={"content-type": "application/pdf"})

    serve(handler)

    with pytest.raises(RuntimeError, match="aborted"):
        await _run(workspace, signal=signal)

    assert _downloads(workspace) == []


async def test_private_address_is_refused(serve, workspace):
    """私网地址走的是 bounded 的真校验:拒绝,且不落任何文件。"""
    serve(_static(PDF), resolves_to="169.254.169.254")

    result = await _run(workspace, url="https://metadata.example/creds.json")

    assert _downloads(workspace) == []
    assert "public internet addresses" in _text(result)


async def test_redirect_into_a_private_address_is_refused(serve, workspace):
    """逐跳校验:第一跳公网、第二跳指向元数据地址,照样拒绝且不落文件。

    请求被钉扎到 IP 后 URL 里的主机名已被替换,所以按 Host 头分辨是哪一跳。
    """

    def handler(request):
        if request.headers.get("host") == "files.example":
            return httpx.Response(302, headers={"location": "http://metadata.internal/latest/meta-data/"})
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    serve(handler, hosts={"metadata.internal": "169.254.169.254"})

    result = await _run(workspace)

    assert _downloads(workspace) == []
    assert "public internet addresses" in _text(result)


async def test_http_error_status_is_reported_without_a_file(serve, workspace):
    """404 之类的失败要说清楚,并且不留文件。"""
    serve(_static(b"nope", content_type="text/html", status=404))

    result = await _run(workspace)

    assert _downloads(workspace) == []
    assert "HTTP 404" in _text(result)


async def test_extension_is_taken_from_the_declared_type_when_the_url_has_none(serve, workspace):
    """URL 没有扩展名时按声明类型补一个,补完照样过 magic 校验。"""
    serve(_static(PDF))

    result = await _run(workspace, url="https://files.example/article/12345")

    assert _downloads(workspace) == ["12345.pdf"]
    assert result.details["bytes"] == len(PDF)


async def test_empty_url_is_rejected_without_a_request(workspace):
    """空 URL 连请求都不该发。"""
    result = await _run(workspace, url="   ")

    assert "needs a URL" in _text(result)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/paper.pdf", "paper.pdf"),
        ("..", ""),
        (".", ""),
        ("", ""),
        (".hidden", "hidden"),
        ("C:\\Windows\\evil.pdf", "evil.pdf"),
        ("pa\u0000per\npaper.pdf", "paperpaper.pdf"),
        ("paper\u202egnp.pdf", "papergnp.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
        ("论文-第一章.pdf", "论文-第一章.pdf"),
    ],
)
def test_filename_sanitizer(raw, expected):
    """路径穿越、绝对路径、控制字符与双向文本覆写全部消掉;非 ASCII 正常字符保留。"""
    assert download_file._sanitize_name(raw) == expected


def test_long_filename_is_capped_but_keeps_its_suffix():
    """超长名按字节截断(CJK 一个字三字节),后缀必须留住——校验规则挂在后缀上。"""
    fitted = download_file._fit_name("论" * 500 + ".pdf")

    assert fitted.endswith(".pdf")
    assert len(fitted.encode()) <= download_file._MAX_NAME_BYTES
    assert fitted[0] == "论"


def test_empty_name_falls_back_to_a_default():
    assert download_file._fit_name("") == "download"


def test_definition_stays_parallel_executable():
    """网络工具不许标 sequential,否则整批工具调用退回串行。"""
    definition = create_download_file_tool_definition("/tmp")

    assert definition.executionMode is None
    assert definition.name == "download_file"


async def test_final_url_query_is_stripped_before_it_reaches_model_or_ledger(serve, workspace):
    """重定向的落点常是带签名的 CDN 直链:签名是模型从没见过的凭据,不许进上下文,也不许进台账。"""

    def handler(request):
        if request.headers.get("host") == "doi.example":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example/a.pdf?X-Amz-Signature=DEADBEEFSECRET&t=1"},
            )
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    serve(handler)

    result = await _run(workspace, url="https://doi.example/10.1/paper")

    assert result.details["final_url"] == "https://cdn.example/a.pdf"
    assert "DEADBEEFSECRET" not in _text(result)
    assert "DEADBEEFSECRET" not in repr(result.details)


async def test_a_request_url_with_a_query_is_not_reported_as_a_redirect(serve, workspace):
    """请求 URL 自带 query 不等于发生了重定向;两边都去掉 query 再比,免得凭空报一次跳转。"""
    serve(_static(PDF))

    result = await _run(workspace, url="https://files.example/paper.pdf?id=7")

    assert result.details["url"] == "https://files.example/paper.pdf?id=7"
    assert "redirected to" not in _text(result)


async def test_nothing_appears_under_the_download_name_until_it_is_verified(serve, workspace):
    """传输途中 downloads/ 里不许出现 paper.pdf:那个名字是「完整且类型已核」的合同。

    半成品被别的工具或姐妹读到时无从分辨,进程被杀也会把截断文件永久留在那个名字下,
    所以先写点号开头的暂存名,校验通过后再原子改名。
    """
    seen: list[list[str]] = []
    directory = workspace / "downloads"

    async def body():
        yield b"%PDF-1.7\n"
        seen.append(sorted(p.name for p in directory.iterdir()))
        yield b"y" * 64

    def handler(_request):
        return httpx.Response(200, content=body(), headers={"content-type": "application/pdf"})

    serve(handler)

    result = await _run(workspace)

    assert seen and "paper.pdf" not in seen[0]
    assert all(name.startswith(".partial-") for name in seen[0])
    # 校验通过后才出现,且暂存文件不留下。
    assert _downloads(workspace) == ["paper.pdf"]
    assert result.details["path"] == str(directory / "paper.pdf")


async def test_bogus_content_length_does_not_escape_as_an_exception(serve, workspace):
    """Content-Length 是远端写的:``'²'.isdigit()`` 为真而 ``int('²')`` 抛错,不许把它变成工具崩溃。"""

    async def body():
        yield PDF

    def handler(_request):
        return httpx.Response(
            200,
            content=body(),
            headers=[(b"content-type", b"application/pdf"), (b"content-length", b"\xc2\xb2")],
        )

    serve(handler)

    result = await _run(workspace)

    # 声明不可解析 → 预检放弃,实际累计仍然是权威,下载照常完成。
    assert _downloads(workspace) == ["paper.pdf"]
    assert result.details["bytes"] == len(PDF)


async def test_percent_encoded_traversal_in_content_disposition_is_sanitized(serve, workspace):
    """RFC 5987 的 ``filename*`` 先要百分号解码,解码后才是路径穿越 —— 消毒必须排在解码之后。"""
    serve(_static(PDF, headers={"content-disposition": "attachment; filename*=UTF-8''%2e%2e%2fevil.pdf"}))

    result = await _run(workspace, url="https://files.example/dl")

    assert _downloads(workspace) == ["evil.pdf"]
    assert not (workspace / "evil.pdf").exists()
    assert result.details["path"] == str(workspace / "downloads" / "evil.pdf")


# --- 下载即入语料(H2)---------------------------------------------------------------

MARKDOWN = ("# 一份笔记\n\n" + "这一段要够长,才算得上有正文的一页。" * 8 + "\n").encode()
MD_URL = "https://files.example/notes.md"


@pytest.fixture
def corpus_root(tmp_path, monkeypatch):
    """每个测试自己的语料库:既不看见别的测试,也不写到开发机的真语料上。"""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    return tmp_path / "corpus"


async def test_a_downloaded_document_is_indexed_and_the_doc_id_comes_back(serve, workspace, corpus_root):
    """真的走一遍 corpus.ingest(.md 不需要 pdftotext):doc_id 进结果也进 details。

    没有这一步,下载 PDF 的下一步就不存在 —— read 工具只认文本和图片。
    """
    serve(_static(MARKDOWN, content_type="text/markdown"))

    result = await _run(workspace, url=MD_URL)

    doc_id = result.details["doc_id"]
    assert _downloads(workspace) == ["notes.md"]
    assert f"indexed as doc {doc_id}" in _text(result)
    assert "doc_outline" in _text(result) and "doc_verify" in _text(result)
    # 归属于本工作区,doc_* 工具(按 workspace 过滤)才找得到它;正文确实入了库。
    assert corpus.resolve_doc(doc_id, workspace=str(workspace))
    assert corpus.verify_quote(doc_id, "这一段要够长", workspace=workspace)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("No text layer found; run OCR first: paper.pdf"),
        OSError("corpus root is read-only"),
    ],
)
async def test_an_ingest_failure_keeps_the_file_and_still_reports_a_successful_download(
    serve, workspace, monkeypatch, error
):
    """扫描件是 ingest 写明的拒收 —— 文件要留着去 OCR,下载本身不许因此报失败。

    OSError 一并盯着:入库是下载之后的额外一步,任何失败的损害范围都只能是它自己,
    否则工具会对着一个已经落盘核过的文件说"下载失败"。
    """

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(corpus, "ingest", refuse)
    serve(_static(PDF))

    result = await _run(workspace)

    assert _downloads(workspace) == ["paper.pdf"]
    assert "Downloaded" in _text(result) and result.details["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert str(error) in _text(result)        # 原因原样转述,模型才知道下一步是 OCR
    assert "doc_id" not in result.details
    # 未入库的 PDF 不许再被指去 read(读出来是乱码):修好之后走 doc_add。
    assert "doc_add" in _text(result) and "use the read tool" not in _text(result)


async def test_an_archive_is_left_alone(serve, workspace, monkeypatch):
    """归档/图片这类语料收不了的类型:一行都不许变,更不许调 ingest。"""
    called = []
    monkeypatch.setattr(corpus, "ingest", lambda *a, **k: called.append(a))
    serve(_static(b"PK\x03\x04" + b"z" * 60, content_type="application/zip"))

    result = await _run(workspace, url="https://files.example/data.zip")

    assert _downloads(workspace) == ["data.zip"]
    assert called == []
    assert "indexed as doc" not in _text(result)
    assert "use the read tool to open it" in _text(result)


async def test_ingest_runs_off_the_event_loop(serve, workspace, monkeypatch):
    """ingest 会 shell 出 pdftotext(超时 300 秒):跑在事件循环上就是把整个会话钉住。"""
    seen: dict[str, threading.Thread] = {}

    def record(path, *_args, **_kwargs):
        seen["thread"] = threading.current_thread()
        assert os.path.isfile(path)          # 传进去的是已落盘、已核过类型的那个文件
        return "0123456789ab", 7

    monkeypatch.setattr(corpus, "ingest", record)
    serve(_static(MARKDOWN, content_type="text/markdown"))

    result = await _run(workspace, url=MD_URL)

    assert seen["thread"] is not threading.main_thread()
    assert result.details["doc_id"] == "0123456789ab"
    assert "(7 pages)" in _text(result)


def test_the_guideline_points_at_the_doc_tools_not_the_read_tool():
    """引导语曾经说"下完用 read 读" —— 对 PDF 是错的建议,而真正的下一步只字未提。"""
    definition = create_download_file_tool_definition("/tmp")
    guidelines = " ".join(definition.promptGuidelines)

    assert "doc_outline" in guidelines and "doc_verify" in guidelines
    assert "Read the saved file afterwards with the read tool" not in guidelines


async def test_symlinked_download_directory_is_rejected_before_request(tmp_path, workspace, monkeypatch):
    outside = tmp_path / 'external'
    outside.mkdir()
    (workspace / 'downloads').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(download_file, 'open_checked_stream', lambda *_a, **_k: pytest.fail('request started'))
    result = await _run(workspace, url=URL)
    assert 'outside the workspace' in _text(result)
    assert list(outside.iterdir()) == []


DJVU = b"AT&TFORM" + b"\x00\x00\x00\xe0" + b"DJVMDIRM" + b"\x00" * 64


async def test_a_djvu_scan_is_a_downloadable_type(serve, workspace):
    """2026-09-18 (B10): CADAL and Wikimedia carry the scanned classics as DjVu, and the tool
    turned them away as "not a downloadable type" while the machine had djvulibre installed."""
    serve(_static(DJVU, content_type="image/vnd.djvu"))

    result = await _run(workspace, url="https://upload.wikimedia.org/wikipedia/commons/x/CADAL06070838.djvu")

    assert "not a downloadable type" not in _text(result)
    assert _downloads(workspace) == ["CADAL06070838.djvu"]
    assert result.details["path"].endswith(".djvu")


async def test_an_extensionless_djvu_takes_its_suffix_from_the_declared_type(serve, workspace):
    serve(_static(DJVU, content_type="image/vnd.djvu"))

    await _run(workspace, url="https://cadal.example/book/06070838")

    assert _downloads(workspace) == ["06070838.djvu"]


async def test_a_file_wearing_djvu_without_its_magic_is_rejected(serve, workspace):
    serve(_static(b"<!DOCTYPE html><html><body>Sign in", content_type="image/vnd.djvu"))

    result = await _run(workspace, url="https://files.example/book.djvu")

    assert _downloads(workspace) == []
    assert "not .djvu content" in _text(result)
