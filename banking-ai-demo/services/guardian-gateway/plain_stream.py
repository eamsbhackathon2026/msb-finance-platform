"""Gỡ markdown khỏi câu trả lời agent NGAY TRONG LÚC chữ chảy qua.

`main._strip_markdown_for_plain` làm đúng việc này nhưng cần cả câu trả lời mới
chạy được. Khi gateway phát trực tiếp từ agent, chữ tới theo từng mẩu nhỏ và
phải đi tiếp tới trình duyệt ngay, nên phần gỡ phải làm được trên dòng chảy.

Cùng bộ quy tắc với bản theo lô: bỏ nguyên dòng bảng markdown (bắt đầu bằng
`|`), gỡ `**đậm**`, `__đậm__`, `#` tiêu đề và `>` trích dẫn, giữ nguyên gạch đầu
dòng. `tests/test_plain_stream.py` so khớp hai bên trên cùng dữ liệu để chúng
không trôi ra khác nhau.

Dấu ĐƠN LẺ được giữ nguyên, không gỡ: `session_flags` phải ra đúng
`session_flags` chứ không phải `sessionflags`. Chỉ cặp hai ký tự mới bị gỡ.

Khác biệt cố ý duy nhất so với bản theo lô: một cặp `**` mở mà không có cặp đóng
thì bản theo lô giữ lại, còn ở đây bị gỡ — lúc đang chảy không thể biết phía sau
có dấu đóng hay không, và để `**` thô chạy ra màn hình thì xấu hơn.
"""
from __future__ import annotations

# Ký tự tạo nên dấu hiệu hai ký tự (**đậm**, __đậm__).
_PAIR_MARKS = ("*", "_")


class PlainTextStreamer:
    """Nhận từng mẩu văn bản, trả lại phần đã sạch và an toàn để phát đi ngay."""

    def __init__(self) -> None:
        self._at_line_start = True
        self._drop_line = False         # đang ở giữa một dòng bảng markdown
        self._indent = ""               # khoảng trắng đầu dòng, chờ biết dòng giữ hay bỏ
        self._pending = ""              # một ký tự dấu hiệu chưa biết có thành cặp không
        self._skip_line_prefix = False  # đang nuốt khoảng trắng sau # hoặc >

    def feed(self, chunk: str) -> str:
        text = self._pending + chunk
        self._pending = ""
        out: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]

            if self._drop_line:
                if ch == "\n":
                    self._drop_line = False
                    self._at_line_start = True
                i += 1
                continue

            if self._at_line_start and self._consume_line_prefix(ch, out):
                i += 1
                continue

            if ch == "\n":
                self._at_line_start = True
                self._skip_line_prefix = False
                out.append(ch)
                i += 1
                continue

            if ch in _PAIR_MARKS:
                if i + 1 >= len(text):
                    # Chưa biết ký tự sau là gì: giữ lại, mẩu kế tiếp quyết định.
                    self._pending = ch
                    break
                if text[i + 1] == ch:
                    i += 2  # cặp dấu hiệu, bỏ cả hai
                    continue
                out.append(ch)  # dấu đơn lẻ là chữ thật, giữ nguyên
                i += 1
                continue

            out.append(ch)
            i += 1
        return "".join(out)

    def _consume_line_prefix(self, ch: str, out: list[str]) -> bool:
        """Xử lý ký tự ở đầu dòng. Trả True nếu ký tự đã được tiêu thụ."""
        if self._skip_line_prefix and ch in " \t":
            # Khoảng trắng ngay sau # hoặc >: thuộc về dấu hiệu, không phải thụt
            # đầu dòng. Phải xét TRƯỚC nhánh thụt đầu dòng bên dưới, nếu không
            # "## Tiêu đề" sẽ ra " Tiêu đề" với một khoảng trắng thừa.
            return True
        if ch in " \t" and len(self._indent) < 3:
            self._indent += ch
            return True
        if ch == "|":
            # Bảng markdown: gateway đã đính bảng số liệu riêng nên bỏ cả dòng,
            # vừa tránh dấu | thô vừa tránh hai bảng chồng nhau.
            self._drop_line = True
            self._indent = ""
            return True
        if ch in "#>":
            self._indent = ""
            self._skip_line_prefix = True
            return True
        if ch == "\n":
            self._indent = ""
            self._skip_line_prefix = False
            out.append(ch)
            return True
        # Ký tự thật đầu tiên của dòng: nhả phần thụt đầu dòng rồi xử lý bình thường.
        out.append(self._indent)
        self._indent = ""
        self._at_line_start = False
        self._skip_line_prefix = False
        return False

    def close(self) -> str:
        """Phần còn lại khi dòng chảy kết thúc."""
        rest = self._pending + self._indent
        self._pending = ""
        self._indent = ""
        return rest
