"""Cho phép test import `main` và `common` từ thư mục service.

Test chạy không cần PostgreSQL: `common.pool()` khởi tạo lười nên chỉ những test
thực sự chạm DB mới cần kết nối, còn lại dùng monkeypatch thay `common.query`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
