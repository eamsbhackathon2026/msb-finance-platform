"""Test bộ lọc dòng bảng theo dòng chảy.

Trước đây file này ghim một bất biến khác: kết quả phải trùng bản gỡ markdown
theo lô, vì cả hai cùng chảy vào bong bóng chat. Bất biến đó hết hiệu lực khi
msb-guardian-fe chuyển sang render markdown thật — chữ nay đi thẳng tới đó, còn
`main._strip_markdown_for_plain` chỉ còn phục vụ màn Guardian (render chữ thuần).

Điều phải giữ bây giờ: markdown đi qua NGUYÊN VẸN để đầu kia render được, riêng
dòng bảng thì bỏ, và việc bỏ ấy phải đúng dù mẩu văn bản bị cắt ở bất kỳ đâu.
"""
from plain_stream import PlainTextStreamer


def run_stream(text: str, size: int) -> str:
    """Đẩy `text` qua bộ lọc theo từng mẩu `size` ký tự."""
    streamer = PlainTextStreamer()
    out = [streamer.feed(text[i:i + size]) for i in range(0, len(text), size)]
    out.append(streamer.close())
    return "".join(out)


GIU_NGUYEN = [
    "Tháng này bạn chi **12.460.000 ₫**, tăng 34% so với tháng trước.",
    "# Tổng quan\n\nChi tiêu đang vượt ngân sách.\n",
    "> Lưu ý: số liệu tính tới hôm nay.\nPhần còn lại giữ nguyên.",
    "* Ăn uống: 4.200.000 ₫\n* Đi lại: 1.100.000 ₫",
    "- Ăn uống\n- Đi lại",
    "*nghiêng*, ~~gạch ngang~~ và [biểu lãi suất](https://msb.com.vn/rates)",
    "Số tài khoản của anh là `5001` ạ.",
    "Trường session_flags và new_device giữ nguyên dấu gạch dưới.",
    "5 * 3 = 15",
    "",
]


def test_markdown_di_qua_nguyen_ven():
    """Mọi dạng markdown phải tới được đầu kia: bên đó mới là nơi render."""
    for sample in GIU_NGUYEN:
        for size in (1, 3, 7, 1000):
            assert run_stream(sample, size) == sample, f"lệch ở mẩu {size} ký tự: {sample!r}"


def test_bo_ca_dong_bang_markdown():
    """Gateway đã đính bảng số liệu riêng, nên bảng markdown là bảng thứ hai thừa."""
    text = "Bảng dưới đây:\n| Nhóm | Số tiền |\n| --- | --- |\n| Ăn uống | 4.200.000 |\nHết bảng."
    for size in (1, 3, 7, 1000):
        ra = run_stream(text, size)
        assert "|" not in ra, f"còn sót dấu bảng ở mẩu {size}: {ra!r}"
        assert ra.startswith("Bảng dưới đây:")
        assert ra.rstrip().endswith("Hết bảng.")


def test_bo_dong_bang_co_thut_dau_dong():
    """Thụt đầu dòng không được biến một dòng bảng thành dòng chữ."""
    for size in (1, 2, 5, 1000):
        assert "|" not in run_stream("Mở đầu\n  | A | B |\nKết.", size)


def test_chu_chay_ra_ngay_khong_cho_het_cau():
    """Bong bóng chat phải thấy chữ ngay, không đợi trọn câu trả lời."""
    streamer = PlainTextStreamer()
    assert streamer.feed("Chào anh") == "Chào anh"


def test_thut_dau_dong_con_treo_duoc_tra_lai_khi_ket_thuc():
    """Mẩu cuối chỉ có khoảng trắng thì khoảng trắng ấy vẫn phải ra."""
    streamer = PlainTextStreamer()
    assert streamer.feed("Xong.\n  ") == "Xong.\n"
    assert streamer.close() == "  "
