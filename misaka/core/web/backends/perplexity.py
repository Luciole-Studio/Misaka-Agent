"""Hermes Perplexity Search API and content snippets, using the session HTTP owner."""

from urllib.parse import urlparse

from misaka.core.web.accounting import account_call
from misaka.core.web.config import provider_env
from misaka.core.web.provider import WebSearchProvider, extraction_error
from misaka.core.web.runtime import api_client


def _query_for_urls(urls):
    words = []
    for url in urls:
        parsed = urlparse(url)
        for token in parsed.path.replace("-", " ").replace("_", " ").replace("/", " ").split():
            if token.lower() not in words and not token.isdigit():
                words.append(token.lower())
        if not parsed.path.strip("/"):
            words.append(parsed.netloc)
    return " ".join(words)[:500] or " ".join(urls)[:500]


async def _request(endpoint, payload, subject, operation):
    key = provider_env("PERPLEXITY_API_KEY")
    if not key:
        raise ValueError("PERPLEXITY_API_KEY is not set; see https://www.perplexity.ai/account/api")
    base = (provider_env("PERPLEXITY_BASE_URL") or "https://api.perplexity.ai").rstrip("/")
    url = f"{base}/{endpoint}"
    async with api_client("perplexity", url, key, timeout=60) as client, account_call(operation, "perplexity", subject):
        response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
    response.raise_for_status()
    result = response.json()
    if isinstance(result, dict) and (result.get("error") or result.get("success") is False):
        raise ValueError("Perplexity rejected the request: " + str(result.get("error") or result.get("message") or "unknown error"))
    if not isinstance(result, dict) or (result.get("results") is not None and not isinstance(result["results"], list)):
        raise ValueError("Perplexity returned an invalid results envelope")
    return result.get("results") or []


class PerplexityWebSearchProvider(WebSearchProvider):
    @property
    def name(self):
        return "perplexity"

    @property
    def display_name(self):
        return "Perplexity"

    def is_available(self):
        return bool(provider_env("PERPLEXITY_API_KEY"))

    def supports_extract(self):
        return True

    async def search(self, query, limit=5):
        try:
            rows = await _request("search", {
                "query": query, "max_results": max(1, min(limit, 20)), "search_context_size": "low",
            }, query, "web_search")
            return {"success": True, "data": {"web": [
                {"title": row.get("title") or "", "url": row.get("url") or "",
                 "description": row.get("snippet") or "", "position": index + 1}
                for index, row in enumerate(rows) if isinstance(row, dict)
            ]}}
        except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
            return {"success": False, "error": f"Perplexity search failed: {error}"}

    async def extract(self, urls, **kwargs):
        if not urls:
            return []
        try:
            rows = await _request("sdk/content/snippets", {
                "query": _query_for_urls(urls), "urls": list(urls),
                "max_tokens": 16384, "max_tokens_per_page": 4096,
            }, ",".join(urls), "web_extract")
            by_url = {row.get("url", ""): row for row in rows if isinstance(row, dict)}
            documents = []
            for url in urls:
                row = by_url.get(url, {})
                text = row.get("text") or ""
                doc = {"url": url, "requested_url": url, "title": "", "content": text, "raw_content": text,
                       "metadata": {"sourceURL": url, "content_kind": "snippets"}}
                if row.get("error") or not text:
                    doc["error"] = str(row.get("error") or "no content returned")
                documents.append(doc)
            return documents
        except Exception as error:  # noqa: BLE001 - tool or transport boundary reports the failure
            return [extraction_error(url, f"Perplexity extract failed: {error}") for url in urls]

    def get_setup_schema(self):
        return {"name": self.display_name, "badge": "paid", "web_tier": "paid",
                "tag": "Ranked web results and query-relevant page snippets (not full-page extraction).",
                "env_vars": [{"key": "PERPLEXITY_API_KEY", "prompt": "Perplexity API key",
                              "url": "https://www.perplexity.ai/account/api"},
                             {"key": "PERPLEXITY_BASE_URL", "prompt": "Optional API base URL", "url": "https://docs.perplexity.ai"}]}
