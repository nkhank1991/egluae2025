"""Optional ScrapeGraphAI enrichment pass for UAE company websites.

Use this after the base UAE company extraction. It is deliberately a second-pass
extractor for unresolved/high-priority companies rather than a 100K first-pass
crawler, because LLM/page rendering cost and latency are much higher.

Install when needed:
  pip install scrapegraphai
  playwright install chromium

Default example uses Ollama locally. Replace the config with another supported
LLM/provider if desired.
"""

import json
from typing import Dict, Any

PROMPT = """
Extract only publicly visible business/professional information from this company's website.
Return JSON with:
- company_name
- ceo_or_managing_director: [{name,title,source_text}]
- marketing_leaders: [{name,title,source_text}]
- brand_leaders: [{name,title,source_text}]
- partnerships_sponsorship_leaders: [{name,title,source_text}]
- commercial_business_development_leaders: [{name,title,source_text}]
- communications_pr_media_leaders: [{name,title,source_text}]
- published_business_emails: []
- published_business_phones: []
- relevant_pages: []
Do not invent names, titles, emails, or phone numbers. If a field is not visible, return an empty list.
"""


def enrich_with_scrapegraph(url: str, model: str = "ollama/llama3.2") -> Dict[str, Any]:
    from scrapegraphai.graphs import SmartScraperGraph

    config = {
        "llm": {
            "model": model,
            "model_tokens": 8192,
            "format": "json",
        },
        "verbose": False,
        "headless": True,
    }
    graph = SmartScraperGraph(prompt=PROMPT, source=url, config=config)
    result = graph.run()
    return result if isinstance(result, dict) else {"raw": result}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--model", default="ollama/llama3.2")
    args = p.parse_args()
    print(json.dumps(enrich_with_scrapegraph(args.url, args.model), indent=2, ensure_ascii=False))
