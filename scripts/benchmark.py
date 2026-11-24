"""Measure real latency, token usage and cost per model on real websites.

Usage (from the project root, with API keys in .env):
    python scripts/benchmark.py https://site1.com https://site2.com https://site3.com
    python scripts/benchmark.py --models gemini:gemini-3.1-flash-lite anthropic:claude-haiku-5-5 -- https://site1.com ...

Prints a markdown table you can paste into the README ("measured", not estimated). It also
reports schema-validity and a few cheap quality checks. These are NOT an accuracy benchmark:
read the outputs yourself and score them before claiming one model is more accurate.
"""
import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
if __import__("os").getenv("USE_SYSTEM_CERTS", "").lower() in ("1", "true", "yes"):
    import truststore  # noqa: E402

    truststore.inject_into_ssl()

from app.config import Settings  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.services.analysis_service import AnalysisService  # noqa: E402
from app.services.llm_service import LLMService  # noqa: E402


async def run_one(provider: str, model: str, urls: list[str], runs: int = 1, save: str | None = None) -> dict:
    import os

    env = dict(os.environ, LLM_PROVIDER=provider, LLM_MODEL=model, LLM_FALLBACK_PROVIDER="", CACHE_TTL_SECONDS="0")
    settings = Settings(env=env)
    service = AnalysisService(settings, LLMService(settings))
    rows, ok = [], 0
    n = 0
    total = len(urls) * runs
    for url in [u for u in urls for _ in range(runs)]:
        try:
            r = await service.analyze(url, refresh=True)
            m = r.meta
            n += 1
            if save:
                import re
                Path(save).mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"\W+", "_", url.split("//")[-1])
                (Path(save) / f"{provider}_{slug}_{n}.json").write_text(r.model_dump_json(indent=1), encoding="utf-8")
            comps = len(r.analysis.competitor_analysis.competitors)
            rows.append((m.llm_ms, m.input_tokens, m.output_tokens, m.estimated_cost_usd or 0.0, comps, len(m.warnings)))
            ok += 1
        except AppError as exc:
            print(f"  [{provider}/{model}] {url} -> {exc.code}: {exc.message}", file=sys.stderr)
    if not rows:
        return {"model": f"{provider}/{model}", "ok": f"0/{total}"}
    col = lambda i: [r[i] for r in rows]  # noqa: E731
    return {
        "model": f"{provider}/{model}", "ok": f"{ok}/{total}",
        "llm_s": f"{statistics.mean(col(0)) / 1000:.1f}", "med_s": f"{statistics.median(col(0)) / 1000:.1f}", "max_s": f"{max(col(0)) / 1000:.1f}", "in": int(statistics.mean(col(1))), "out": int(statistics.mean(col(2))),
        "cost": f"${statistics.mean(col(3)):.5f}", "comps": f"{statistics.mean(col(4)):.1f}", "warn": f"{statistics.mean(col(5)):.1f}",
    }


async def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["gemini:gemini-3.1-flash-lite"], help="provider:model pairs")
    ap.add_argument("--runs", type=int, default=1, help="repeats per site")
    ap.add_argument("--save", help="write per-run JSON outputs to this folder")
    ap.add_argument("urls", nargs="+")
    args = ap.parse_args()
    results = []
    for pair in args.models:
        provider, model = pair.split(":", 1)
        print(f"Running {pair} on {len(args.urls)} sites...", file=sys.stderr)
        results.append(await run_one(provider, model, args.urls, args.runs, args.save))
    print("\n| Model | Valid runs | Avg LLM s | Median s | Max s | Avg input tok | Avg output tok | Avg cost / analysis | Avg competitors | Avg warnings |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['model']} | {r['ok']} | {r.get('llm_s', '-')} | {r.get('med_s', '-')} | {r.get('max_s', '-')} | {r.get('in', '-')} | {r.get('out', '-')} | {r.get('cost', '-')} | {r.get('comps', '-')} | {r.get('warn', '-')} |")


if __name__ == "__main__":
    asyncio.run(main())
