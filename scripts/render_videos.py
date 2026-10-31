"""Render the video of every listing with video turned on that is still waiting, then exit.

    python -m scripts.render_videos

The web server does the same work slowly in the background (VIDEO_WORKER). Run this from a faster
machine with DATABASE_URL set to clear a backlog, or as a scheduled job on hosts that pause
background threads between requests, such as Cloud Run (set VIDEO_WORKER=0 there).
"""
from app import video


def main() -> None:
    count = 0
    while video.render_next():
        count += 1
    print(f"Processed {count} video(s); nothing is waiting now.")


if __name__ == "__main__":
    main()
