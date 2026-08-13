"""Sister 的文献工具：树导航式读长文献 + 单正典引文复核。

闭环＝PageIndex 的本意：**看目录 → 选章节 → 取该节原文**，而不是 grep 出碎片。
挂载：worker/子代理把 register 直接传进会话（extension_factories）。
"""
import os
import sys

from misaka.core.extensions.types import ToolDefinition
from pydantic import BaseModel, Field

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

from misaka.research.indexer import index as corpus  # noqa: E402

def _text(s):
    return {"content": [{"type": "text", "text": s}], "details": {}}


def _register(harn, name, label, description, parameters, snippet=None, guidelines=None):
    def deco(fn):
        async def execute(tool_call_id, raw, signal, on_update, ctx):
            args = raw if isinstance(raw, parameters) else parameters(**(raw or {}))
            return await fn(tool_call_id, args, signal, on_update, ctx)
        harn.registerTool(ToolDefinition(
            name=name, label=label, description=description,
            parameters=parameters.model_json_schema(), execute=execute,
            promptSnippet=snippet, promptGuidelines=list(guidelines or [])))
        return fn
    return deco


def register(harn):
    class ListParams(BaseModel):
        query: str = Field("", description="按标题筛选，留空列全部")

    @_register(
        harn, name="doc_list", label="文献清单",
        description="列出工作区里已入库的文献（研究材料与各卡产物），给出 doc_id。",
        snippet="列出可读的文献与 doc_id",
        parameters=ListParams)
    async def doc_list(tool_call_id, params, signal, on_update, ctx):
        rows = corpus.docs()
        if params.query:
            rows = [r for r in rows if params.query.lower() in (r["title"] or "").lower()]
        if not rows:
            return _text("（工作区暂无入库文献）")
        return _text("\n".join(f"{r['doc_id']}  {r['pages']:>4} 页  {r['title']}" for r in rows))

    class OutlineParams(BaseModel):
        doc_id: str = Field(description="文献 id（用 doc_list 查）")

    @_register(
        harn, name="doc_outline", label="文献目录",
        description="读一份文献的**结构目录**（章节标题+页范围+节点号）。读长文献必须先看这个。",
        snippet="看文献目录树（先看目录再定位）",
        guidelines=[
            "读长文献时先用 doc_outline 看目录，再用 doc_read 取相关章节——不要一上来就全文检索碎片。",
        ],
        parameters=OutlineParams)
    async def doc_outline(tool_call_id, params, signal, on_update, ctx):
        o = corpus.tree_outline(params.doc_id)
        if o:
            return _text(o + "\n\n（用 doc_read(doc_id, node=节点号) 取某一节原文）")
        st = corpus.structure(params.doc_id)
        if not st:
            return _text("无此文献（先用 doc_list 查 doc_id）")
        heads = "\n".join(f"  p{p['page']}  {p['head']}" for p in st.get("pages", [])[:80])
        return _text(f"# {st['title']}（无结构树，按页导航）\n{heads}")

    class ReadParams(BaseModel):
        doc_id: str = Field(description="文献 id")
        node: str = Field("", description="目录里的节点号（如 0013），优先用这个")
        pages: str = Field("", description="页区间如 '32-40'；node 为空时用")

    @_register(
        harn, name="doc_read", label="读文献",
        description="取文献某一节/某几页的**原文**。优先给 node（目录里的节点号），或给 pages 区间。",
        snippet="按目录节点或页区间取文献原文",
        parameters=ReadParams)
    async def doc_read(tool_call_id, params, signal, on_update, ctx):
        if params.node:
            span = corpus.node_pages(params.doc_id, params.node)
            if not span:
                return _text(f"目录里没有节点 {params.node}（先 doc_outline）")
            start, end = span
        elif params.pages:
            try:
                a, _, b = params.pages.partition("-")
                start, end = int(a), int(b or a)
            except ValueError:
                return _text("pages 格式应为 '32-40' 或 '32'")
        else:
            return _text("需要 node 或 pages 之一")
        txt = corpus.read_pages(params.doc_id, start, end)
        return _text(txt or f"p{start}-{end} 无文本（可能是图版页）")

    class FindParams(BaseModel):
        query: str = Field(description="检索词")
        doc_id: str = Field("", description="限定某份文献，留空则全库")

    @_register(
        harn, name="doc_find", label="文献检索",
        description="在正典里逐字检索（返回 doc_id + 页码 + 片段）。**碎片只用于定位**，读内容仍走 doc_read。",
        snippet="在文献正典里逐字检索定位",
        parameters=FindParams)
    async def doc_find(tool_call_id, params, signal, on_update, ctx):
        hits = corpus.search_literal(params.query, doc_id=params.doc_id or None)
        if not hits:
            return _text("0 命中")
        return _text("\n".join(f"{h['doc_id']} p{h['page']}  {h['s'][:100]}" for h in hits))

    class VerifyParams(BaseModel):
        doc_id: str = Field(description="文献 id")
        quote: str = Field(description="要复核的逐字引文")

    @_register(
        harn, name="doc_verify", label="引文复核",
        description="复核一句引文是否**真在**该文献里，并给出页码与 claim_hash。引用进产物前必须过这一关。",
        snippet="复核引文真实性并取页锚与 claim_hash",
        guidelines=[
            "任何引自文献的原话，写进产物前先用 doc_verify 复核；核不出就不要引用，改写成自己的话或标注未能确证。",
        ],
        parameters=VerifyParams)
    async def doc_verify(tool_call_id, params, signal, on_update, ctx):
        v = corpus.verify_quote(params.doc_id, params.quote)
        if not v:
            return _text("❌ 这句话不在该文献里——**不要引用它**（改写或标注未能确证）")
        return _text(f"✅ p{v['page']} 第 {v['offset']} 字\nclaim_hash {v['claim_hash']}\n"
                     f"（引用时标注：[{params.doc_id} p{v['page']}]）")


