"""HERMES OMEGA - Suite de Validation"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    duration_ms: float = 0.0
    error: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class ModuleResult:
    module_name: str
    weight: float
    tests: List[TestResult] = field(default_factory=list)
    score: float = 0.0

    def compute_score(self) -> float:
        if not self.tests:
            return 0.0
        passed = sum(1 for t in self.tests if t.passed)
        return (passed / len(self.tests)) * self.weight


@dataclass
class SuiteReport:
    modules: List[ModuleResult] = field(default_factory=list)
    total_duration_ms: float = 0.0
    global_score: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "global_score_percent": round(self.global_score, 2),
            "total_duration_ms": round(self.total_duration_ms, 2),
            "modules": [
                {
                    "name": m.module_name,
                    "weight": m.weight,
                    "score": round(m.score, 2),
                    "tests": [
                        {
                            "name": t.name,
                            "passed": t.passed,
                            "duration_ms": round(t.duration_ms, 2),
                            "error": t.error,
                            "detail": t.detail,
                        }
                        for t in m.tests
                    ],
                }
                for m in self.modules
            ],
        }


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def http_get(
    session: aiohttp.ClientSession,
    url: str,
    timeout: float = 10.0,
    **kwargs: Any,
) -> TestResult:
    name = kwargs.pop("test_name", f"GET {url}")
    try:
        start = time.perf_counter()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            status = resp.status
            body = await resp.json()
        elapsed = (time.perf_counter() - start) * 1000
        if status >= 200 and status < 300:
            return TestResult(name=name, passed=True, duration_ms=elapsed, detail=str(body)[:120])
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=f"HTTP {status}", detail=str(body)[:120])
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000 if "start" in dir() else 0
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=str(exc)[:200])


async def http_post(
    session: aiohttp.ClientSession,
    url: str,
    payload: Any,
    timeout: float = 10.0,
    **kwargs: Any,
) -> TestResult:
    name = kwargs.pop("test_name", f"POST {url}")
    try:
        start = time.perf_counter()
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            status = resp.status
            body = await resp.json()
        elapsed = (time.perf_counter() - start) * 1000
        if status >= 200 and status < 300:
            return TestResult(name=name, passed=True, duration_ms=elapsed, detail=str(body)[:120])
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=f"HTTP {status}", detail=str(body)[:120])
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000 if "start" in dir() else 0
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=str(exc)[:200])

async def http_get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    test_name: str = "",
    max_retries: int = 6,
    retry_delay: float = 3.0,
    timeout: float = 10.0,
) -> TestResult:
    """Like http_get but retries on connection errors (for slow-starting modules)."""
    name = test_name or f"GET {url}"
    last_result: Optional[TestResult] = None
    for attempt in range(max_retries):
        last_result = await http_get(session, url, timeout=timeout, test_name=name)
        if last_result.passed:
            return last_result
        if "Cannot connect" in (last_result.error or "") or "timed out" in (last_result.error or ""):
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                continue
        break
    return last_result


# ---------------------------------------------------------------------------
# Redis check (raw TCP ping)
# ---------------------------------------------------------------------------

async def check_redis(session: aiohttp.ClientSession) -> TestResult:
    name = "Redis ping"
    try:
        start = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 6379), timeout=5.0
        )
        writer.write(b"PING\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        writer.close()
        await writer.wait_closed()
        elapsed = (time.perf_counter() - start) * 1000
        resp = data.decode().strip()
        if resp == "+PONG":
            return TestResult(name=name, passed=True, duration_ms=elapsed, detail=resp)
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=f"Unexpected response: {resp}")
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000 if "start" in dir() else 0
        return TestResult(name=name, passed=False, duration_ms=elapsed, error=str(exc)[:200])


# ---------------------------------------------------------------------------
# Module validators
# ---------------------------------------------------------------------------

async def validate_dependencies(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Dependencies", weight=15.0)
    mod.tests.append(await check_redis(session))
    mod.tests.append(await http_get(session, "http://localhost:6333/collections", test_name="Qdrant HTTP"))
    mod.tests.append(await http_get(session, "http://localhost:11434/api/tags", test_name="Ollama HTTP"))
    mod.score = mod.compute_score()
    return mod


async def validate_brain(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Omega Brain", weight=15.0)
    mod.tests.append(await http_get(session, "http://localhost:9300/status", test_name="Brain /status"))
    mod.tests.append(await http_get(session, "http://localhost:9300/health", test_name="Brain /health"))
    mod.tests.append(await http_post(
        session, "http://localhost:9300/think",
        payload={"query": "What is 2+2?"},
        timeout=180.0,
        test_name="Brain /think",
    ))
    mod.score = mod.compute_score()
    return mod


async def validate_knowledge(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Knowledge Graph", weight=15.0)
    mod.tests.append(await http_get(session, "http://localhost:9306/health", test_name="Knowledge /health"))
    mod.tests.append(await http_get(session, "http://localhost:9306/collections", test_name="Knowledge /collections"))
    mod.tests.append(await http_post(
        session, "http://localhost:9306/store/hermes_val",
        payload={"content": "test document", "metadata": {"source": "validation"}},
        test_name="Knowledge /store",
    ))
    mod.tests.append(await http_post(
        session, "http://localhost:9306/search/hermes_val",
        payload={"query": "test", "limit": 5},
        test_name="Knowledge /search",
    ))
    mod.score = mod.compute_score()
    return mod


async def validate_computer_use(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Computer Use", weight=15.0)
    mod.tests.append(await http_get(session, "http://localhost:9307/health", test_name="ComputerUse /health"))
    mod.tests.append(await http_get(session, "http://localhost:9307/status", test_name="ComputerUse /status"))
    mod.tests.append(await http_get(session, "http://localhost:9307/windows", test_name="ComputerUse /windows"))
    mod.tests.append(await http_get(session, "http://localhost:9307/clipboard", test_name="ComputerUse /clipboard"))
    mod.tests.append(await http_post(
        session, "http://localhost:9307/command/execute",
        payload={"command": "echo test", "timeout": 10},
        test_name="ComputerUse /command/execute",
    ))
    mod.tests.append(await http_get(session, "http://localhost:9307/file/list?path=.", test_name="ComputerUse /file/list"))
    mod.score = mod.compute_score()
    return mod


async def validate_nexus(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Nexus Bus", weight=10.0)
    mod.tests.append(await http_get(session, "http://localhost:9305/status", test_name="Nexus /status"))
    mod.tests.append(await http_get(session, "http://localhost:9305/channels", test_name="Nexus /channels"))
    mod.tests.append(await http_post(
        session, "http://localhost:9305/publish/system",
        payload={"data": {"test": "hello"}},
        test_name="Nexus /publish/system",
    ))
    mod.score = mod.compute_score()
    return mod


async def validate_evolution(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Evolution", weight=10.0)
    mod.tests.append(await http_get(session, "http://localhost:9304/status", test_name="Evolution /status"))
    mod.tests.append(await http_post(
        session, "http://localhost:9304/propose",
        payload={"description": "test", "target": "test", "changes": {}},
        test_name="Evolution /propose",
    ))
    mod.tests.append(await http_get(session, "http://localhost:9304/audit", test_name="Evolution /audit"))
    mod.score = mod.compute_score()
    return mod


async def validate_tech_watcher(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Tech Watcher", weight=8.0)
    mod.tests.append(await http_get_with_retry(session, "http://localhost:9301/status", test_name="Watcher /status", max_retries=8, retry_delay=3.0))
    mod.tests.append(await http_get_with_retry(session, "http://localhost:9301/health", test_name="Watcher /health", max_retries=4, retry_delay=2.0))
    mod.tests.append(await http_get_with_retry(session, "http://localhost:9301/results", test_name="Watcher /results", max_retries=4, retry_delay=2.0))
    mod.score = mod.compute_score()
    return mod


async def validate_scraper(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Scraper Engine", weight=7.0)
    mod.tests.append(await http_get(session, "http://localhost:9302/status", test_name="Scraper /status"))
    mod.tests.append(await http_get(session, "http://localhost:9302/health", test_name="Scraper /health"))
    mod.tests.append(await http_post(
        session, "http://localhost:9302/scrape",
        payload={"url": "https://example.com", "source": "test"},
        test_name="Scraper /scrape",
    ))
    mod.score = mod.compute_score()
    return mod


async def validate_genesis(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Genesis", weight=5.0)
    mod.tests.append(await http_get(session, "http://localhost:9303/status", test_name="Genesis /status"))
    mod.tests.append(await http_get(session, "http://localhost:9303/health", test_name="Genesis /health"))
    mod.tests.append(await http_get(session, "http://localhost:9303/agents", test_name="Genesis /agents"))
    mod.score = mod.compute_score()
    return mod


async def validate_integration(session: aiohttp.ClientSession) -> ModuleResult:
    mod = ModuleResult(module_name="Integration", weight=5.0)

    # brain -> knowledge chain
    try:
        start = time.perf_counter()
        async with session.post(
            "http://localhost:9300/think",
            json={"query": "test integration"},
            timeout=aiohttp.ClientTimeout(total=180.0),
        ) as resp:
            think_ok = 200 <= resp.status < 300
            think_body = await resp.json()
        elapsed_think = (time.perf_counter() - start) * 1000

        store_ok = False
        if think_ok:
            response_text = think_body.get("response", "")
            async with session.post(
                "http://localhost:9306/store/hermes_val",
                json={"content": response_text, "metadata": {"source": "integration_test"}},
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp2:
                store_ok = 200 <= resp2.status < 300
        elapsed = (time.perf_counter() - start) * 1000
        if think_ok and store_ok:
            mod.tests.append(TestResult(name="Brain->Knowledge chain", passed=True, duration_ms=elapsed))
        else:
            mod.tests.append(TestResult(
                name="Brain->Knowledge chain", passed=False, duration_ms=elapsed,
                error=f"think_ok={think_ok}, store_ok={store_ok}",
            ))
    except Exception as exc:
        mod.tests.append(TestResult(name="Brain->Knowledge chain", passed=False, error=str(exc)[:200]))

    # nexus publish
    try:
        start = time.perf_counter()
        async with session.post(
            "http://localhost:9305/publish/system",
            json={"data": {"validation": "integration", "ts": time.time()}},
            timeout=aiohttp.ClientTimeout(total=10.0),
        ) as resp:
            pub_ok = 200 <= resp.status < 300
        elapsed = (time.perf_counter() - start) * 1000
        mod.tests.append(TestResult(
            name="Nexus publish/sub roundtrip", passed=pub_ok, duration_ms=elapsed,
        ))
    except Exception as exc:
        mod.tests.append(TestResult(name="Nexus publish/sub roundtrip", passed=False, error=str(exc)[:200]))

    mod.score = mod.compute_score()
    return mod


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

VALIDATORS = [
    validate_dependencies,
    validate_brain,
    validate_knowledge,
    validate_computer_use,
    validate_nexus,
    validate_evolution,
    validate_tech_watcher,
    validate_scraper,
    validate_genesis,
    validate_integration,
]


def render_bar(score: float, width: int = 30) -> str:
    filled = int(score / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def print_report(report: SuiteReport) -> None:
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  HERMES OMEGA - VALIDATION REPORT")
    print(f"  {report.timestamp}")
    print(f"{sep}")

    for mod in report.modules:
        pct = mod.score
        icon = "+" if pct == mod.weight else ("~" if pct > 0 else "-")
        total_tests = len(mod.tests)
        passed_tests = sum(1 for t in mod.tests if t.passed)
        print(f"\n  [{icon}] {mod.module_name}  (weight={mod.weight}%)  {passed_tests}/{total_tests} tests")
        print(f"      Score: {pct:.1f}/{mod.weight:.1f}  {render_bar(pct, 30)}")
        for t in mod.tests:
            mark = "PASS" if t.passed else "FAIL"
            dur = f"{t.duration_ms:.0f}ms"
            line = f"        {mark}  {t.name}  ({dur})"
            if t.error:
                line += f"  >> {t.error}"
            if t.detail and t.passed:
                line += f"  -- {t.detail}"
            print(line)

    print(f"\n{sep}")
    print(f"  GLOBAL SCORE: {report.global_score:.1f}%  {render_bar(report.global_score, 40)}")
    print(f"  Total duration: {report.total_duration_ms:.0f}ms")
    print(f"{sep}\n")


async def run_suite() -> SuiteReport:
    report = SuiteReport(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        start_total = time.perf_counter()
        tasks = [validator(session) for validator in VALIDATORS]
        report.modules = await asyncio.gather(*tasks)
        report.total_duration_ms = (time.perf_counter() - start_total) * 1000

    report.global_score = sum(m.score for m in report.modules)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="HERMES OMEGA - Validation Suite")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Export results as JSON to stdout")
    args = parser.parse_args()

    print("Starting HERMES OMEGA validation suite...")
    report = asyncio.run(run_suite())

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
