"""Cho phép test import `main`, `models`, `catalog` từ thư mục service.

Gateway không dùng `services/_common/common.py` vì không chạm database và chưa
gọi service nào — nên cũng không nằm trong `make sync-common`.

Tắt hai khoảng chờ cố ý (nhịp 2s của màn phân tích và 25ms mỗi token chat) để
bộ test chạy trong tích tắc thay vì vài chục giây.
"""
import os
import sys

os.environ.setdefault("RISK_ASSESS_DELAY_MS", "0")
os.environ.setdefault("CHAT_TOKEN_DELAY_MS", "0")

sys.path.insert(0, os.path.dirname(__file__))
