"""Run the market check for every listing that is due, then exit.

    python -m scripts.market_check

Uses model and web search quota for each listing. A failure stops the run, because the
usual cause is a quota that the remaining listings would hit too.
"""

from app.services import market


def main() -> None:
    done = 0
    while (result := market.check_next()) == "done":
        done += 1
    print(f"Checked {done} listing(s)." + (" Stopped after a failure; see above." if result == "failed" else ""))


if __name__ == "__main__":
    main()
