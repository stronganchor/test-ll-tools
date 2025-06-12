#!/usr/bin/env python
"""
test_flashcard_quiz.py
──────────────────────
Self-contained smoke-tester for Language-Learner-Tools “flash-card” quizzes.

  • Launches Chrome (headless by default, --show for GUI)
  • Tests the first N categories on each supplied page
  • Clicks *immediately* once (to expose race-conditions), then after audio
  • Prints PASS / FAIL / SKIP for every (page, category) combo
  • On FAIL or SKIP shows a compact step-by-step trace
  • Suppresses DevTools / absl / TensorFlow chatter
  • Handles connection errors and Ctrl-C gracefully
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import urlparse

# ─── third-party ──────────────────────────────────────────────────────────
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

try:
    from colorama import Fore, Style, init as _cinit

    _cinit()
    _COLOUR = True
except ModuleNotFoundError:  # colour optional
    _COLOUR = False
    Fore = Style = type("Dummy", (), {"GREEN": "", "RED": "", "YELLOW": "", "RESET_ALL": ""})


# ─── CONFIG ───────────────────────────────────────────────────────────────
QUIZ_PAGES = [
    "https://starter-english-test.local/home/",
    "https://www.turkishtextbook.com/vocab-lessons/",
    "https://starterenglish.com/",
    "https://wordboat.com/biblical-hebrew/",
]

MAX_CATEGORY_TESTS = 5           # first N categories per site
ROUNDS_PER_RUN = 3
MAX_PAGE_LOAD_SEC = 20
MAX_ROUND_SEC = 25
ROUND_START_DELAY = 0.2          # after audio reached ≥0.4 s


# ─── small helpers ────────────────────────────────────────────────────────
class StepLog:
    """Collects *successful* milestones so we can print them after a failure."""

    def __init__(self) -> None:
        self._steps: List[str] = []

    def add(self, msg: str) -> None:
        self._steps.append(msg)

    def dump(self) -> str:
        return "\n    ".join(self._steps)


def _short_exc(ex: Exception, maxlen: int = 160) -> str:
    txt = str(ex).strip().splitlines()[0]
    return txt[: maxlen - 1] + "…" if len(txt) > maxlen else txt


def _colour(s: str, col: str) -> str:
    return f"{col}{s}{Style.RESET_ALL}" if _COLOUR else s


def _host_short(url: str) -> str:
    h = urlparse(url).hostname or "site"
    return h.replace("www.", "").split(".")[0]


@dataclass
class Result:
    url: str
    category: int
    verdict: str   # PASS | FAIL | SKIP
    detail: str
    elapsed: float
    steps: StepLog


class SkipTest(Exception):
    """Raised when a test should be skipped without counting as failure."""


# ─── Selenium helpers ─────────────────────────────────────────────────────
@contextmanager
def browser(headless: bool, quiet: bool = True):
    """
    Spin up Chrome → yield driver → quit.

    When *quiet* is True, **both** stdout & stderr of Chromedriver/Chrome are
    sent to os.devnull for the whole session, killing DevTools / absl noise.
    """
    opts = ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-logging")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    devnull = open(os.devnull, "w") if quiet else None  # keep handle open
    redir_out = contextlib.redirect_stdout(devnull) if quiet else contextlib.nullcontext()
    redir_err = contextlib.redirect_stderr(devnull) if quiet else contextlib.nullcontext()

    with redir_out, redir_err:
        try:
            service = Service(
                ChromeDriverManager().install(),
                log_output=devnull if quiet else None,  # type: ignore[arg-type]
            )
            drv = Chrome(service=service, options=opts)
        except Exception as ex:
            if devnull:
                devnull.close()
            raise SkipTest(_short_exc(ex))

    try:
        yield drv
    finally:
        drv.quit()
        if devnull:
            devnull.close()


def wait_click(drv, by, value, timeout=MAX_PAGE_LOAD_SEC):
    elm = WebDriverWait(drv, timeout).until(EC.element_to_be_clickable((by, value)))
    drv.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", elm)
    elm.click()
    return elm


def flashcards_visible(drv, min_cards=2) -> bool:
    cards = drv.find_elements(By.CSS_SELECTOR, ".flashcard-container")
    return len([c for c in cards if c.is_displayed()]) >= min_cards


def wait_pointer_enabled(drv, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        pe = drv.execute_script(
            "var c=document.querySelector('#ll-tools-flashcard');"
            "return c?getComputedStyle(c).pointerEvents:null;"
        )
        if pe and pe != "none":
            return
        time.sleep(0.1)
    raise TimeoutException("pointer-events never enabled")


def wait_audio_played(drv, min_time=0.4, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        ct = drv.execute_script(
            "var a=document.querySelector('#ll-tools-flashcard audio');"
            "return a?a.currentTime:null;"
        )
        if ct and ct >= min_time:
            return
        time.sleep(0.1)
    raise TimeoutException("audio never reached {:.1f}s".format(min_time))


def choose_single_category(drv, idx: int, log: StepLog):
    boxes = drv.find_elements(
        By.CSS_SELECTOR, "#ll-tools-category-checkboxes input[type='checkbox']"
    )
    if not boxes:
        raise SkipTest("page auto-starts without category picker")

    if idx >= len(boxes):
        raise SkipTest(f"only {len(boxes)} categories on page")

    for cb in boxes:
        if cb.is_selected():
            cb.click()
    boxes[idx].click()
    log.add(f"category #{idx} selected")

    start_btn = drv.find_element(By.ID, "ll-tools-start-selected-quiz")
    if not start_btn.is_enabled():
        raise SkipTest("Start button disabled after category change")
    start_btn.click()
    log.add("quiz started")


def early_click_first_card(drv, log: StepLog):
    for c in drv.find_elements(By.CSS_SELECTOR, ".flashcard-container"):
        try:
            if c.is_displayed():
                c.click()
                log.add("early click sent")
                return
        except (StaleElementReferenceException, ElementClickInterceptedException):
            continue


# ─── core test routine ────────────────────────────────────────────────────
def run_quiz(url: str, cat_idx: int, log: StepLog, headless: bool):
    with browser(headless=headless) as d:
        try:
            d.get(url)
            log.add("page loaded")
        except WebDriverException as ex:
            raise SkipTest(_short_exc(ex))

        # open quiz popup
        try:
            wait_click(d, By.ID, "ll-tools-start-flashcard")
            log.add("start-button clicked")
        except TimeoutException:
            log.add("start-button not present (auto popup)")

        choose_single_category(d, cat_idx, log)

        for rnd in range(1, ROUNDS_PER_RUN + 1):
            if not WebDriverWait(d, MAX_ROUND_SEC).until(flashcards_visible):
                raise RuntimeError(f"round {rnd}: flashcards never appeared")
            log.add(f"round {rnd} cards visible")

            early_click_first_card(d, log)
            wait_pointer_enabled(d)
            wait_audio_played(d)
            log.add(f"round {rnd} audio ≥0.4 s")

            time.sleep(ROUND_START_DELAY)

            t0 = time.time()
            while time.time() - t0 < MAX_ROUND_SEC:
                cards = d.find_elements(By.CSS_SELECTOR, ".flashcard-container")
                progress = False
                for c in cards:
                    try:
                        if c.is_displayed():
                            c.click()
                            progress = True
                            time.sleep(0.35)
                    except StaleElementReferenceException:
                        continue

                if not progress or not flashcards_visible(d):
                    break
            else:
                raise RuntimeError(f"round {rnd}: never completed")
            log.add(f"round {rnd} completed")


# ─── CLI / runner ─────────────────────────────────────────────────────────
def _case_matrix() -> List[Tuple[str, int]]:
    return [(u, ci) for u in QUIZ_PAGES for ci in range(MAX_CATEGORY_TESTS)]


def _argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Flash-card quiz smoke-tester")
    p.add_argument("--show", action="store_true", help="run with visible Chrome")
    p.add_argument("--no-colour", action="store_true", help="disable ANSI colours")
    return p


def _print_result(label: str, res: Result):
    col = {"PASS": Fore.GREEN, "FAIL": Fore.RED, "SKIP": Fore.YELLOW}.get(res.verdict, "")
    print(f"{label:<28} … {_colour(res.verdict, col)} ({res.elapsed:5.1f}s) {res.detail}")
    if res.verdict != "PASS":
        for line in res.steps.dump().splitlines():
            print(f"    {line}")


def main(argv: List[str] | None = None) -> None:
    args = _argparser().parse_args(argv)
    if args.no_colour or not _COLOUR:
        for attr in vars(Fore) | vars(Style):
            if isinstance(getattr(Fore, attr, ""), str):
                setattr(Fore, attr, "")
            if isinstance(getattr(Style, attr, ""), str):
                setattr(Style, attr, "")

    headless = not args.show
    all_results: List[Result] = []
    suite_t0 = time.time()

    try:
        for url, cat in _case_matrix():
            label = f"{_host_short(url)}-cat{cat}"
            log = StepLog()
            t0 = time.time()
            try:
                run_quiz(url, cat, log, headless)
            except SkipTest as ex:
                verdict, detail = "SKIP", str(ex)
            except Exception as ex:
                verdict, detail = "FAIL", _short_exc(ex)
            else:
                verdict, detail = "PASS", ""
            elapsed = time.time() - t0
            res = Result(url, cat, verdict, detail, elapsed, log)
            _print_result(label, res)
            all_results.append(res)
    except KeyboardInterrupt:
        print("\n▶  Interrupted by user – summarising…\n")

    # summary
    passed = sum(r.verdict == "PASS" for r in all_results)
    failed = sum(r.verdict == "FAIL" for r in all_results)
    skipped = sum(r.verdict == "SKIP" for r in all_results)
    print(
        f"\n{_colour(str(passed), Fore.GREEN)} passed, "
        f"{_colour(str(failed), Fore.RED)} failed, "
        f"{_colour(str(skipped), Fore.YELLOW)} skipped "
        f"• total {time.time() - suite_t0:0.1f}s"
    )
    sys.exit(130 if failed == 0 and skipped == 0 else (1 if failed else 0))


if __name__ == "__main__":
    main()
