"""Cho phép test import `main`, `models`, `catalog` từ thư mục service.

Gateway không dùng `services/_common/common.py` vì không chạm database và chưa
gọi service nào — nên cũng không nằm trong `make sync-common`.

Tắt hai khoảng chờ cố ý (nhịp 2s của màn phân tích và 25ms mỗi token chat) để
bộ test chạy trong tích tắc thay vì vài chục giây.
"""
import os
import sys

# Test chạy tách khỏi 5 service domain: chúng không tồn tại trong môi trường
# test, và nếu để bật thì mỗi lời gọi phải chờ hết timeout rồi mới rơi về dữ
# liệu tạm — bộ test sẽ mất hàng phút và kết quả phụ thuộc mạng.
# Phần ánh xạ domain được kiểm tra riêng bằng hàm thuần trong test_mappers.py.
os.environ.setdefault("DOMAIN_ENABLED", "false")

os.environ.setdefault("RISK_ASSESS_DELAY_MS", "0")
os.environ.setdefault("CHAT_TOKEN_DELAY_MS", "0")

sys.path.insert(0, os.path.dirname(__file__))
