"""Lọc dòng bảng khỏi câu trả lời agent NGAY TRONG LÚC chữ chảy qua.

Trước đây file này gỡ cả markdown thành chữ thuần, vì bong bóng chat render văn
bản thô. Bộ luật viết tay ấy luôn thiếu: `*nghiêng*`, `~~gạch ngang~~` và cả URL
trong `[nhãn](url)` đều lọt ra màn hình dưới dạng dấu lạ, và mỗi dạng mới lại
phải viết hai lần — một cho bản theo lô, một cho bản đọc từng ký tự ở đây.

Nay msb-guardian-fe render markdown thật, nên chữ đi thẳng tới đó. Việc duy nhất
còn lại là BỎ DÒNG BẢNG: gateway tự dựng bảng số liệu riêng từ số của domain và
đính kèm, nên để bảng markdown chạy qua nữa thì màn hình có hai bảng nói cùng
một chuyện.

Vẫn phải làm trên dòng chảy vì chữ tới theo từng mẩu nhỏ và phải đi tiếp tới
trình duyệt ngay, chứ không đợi hết câu.
"""
from __future__ import annotations


class PlainTextStreamer:
    """Nhận từng mẩu văn bản, trả lại phần an toàn để phát đi ngay."""

    def __init__(self) -> None:
        self._at_line_start = True
        self._drop_line = False   # đang ở giữa một dòng bảng markdown
        self._indent = ""         # khoảng trắng đầu dòng, chờ biết dòng giữ hay bỏ

    def feed(self, chunk: str) -> str:
        out: list[str] = []
        for ch in chunk:
            if self._drop_line:
                if ch == "\n":
                    self._drop_line = False
                    self._at_line_start = True
                continue

            if self._at_line_start:
                if ch in " \t" and len(self._indent) < 3:
                    # Thụt đầu dòng: giữ lại đã, vì dòng có thể là dòng bảng và bị bỏ cả.
                    self._indent += ch
                    continue
                if ch == "|":
                    self._drop_line = True
                    self._indent = ""
                    continue
                if ch == "\n":
                    self._indent = ""
                    out.append(ch)
                    continue
                out.append(self._indent)
                self._indent = ""
                self._at_line_start = False

            if ch == "\n":
                self._at_line_start = True
            out.append(ch)
        return "".join(out)

    def close(self) -> str:
        """Phần thụt đầu dòng còn treo khi dòng chảy kết thúc."""
        rest = self._indent
        self._indent = ""
        return rest
