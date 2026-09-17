"""Test bộ gỡ markdown theo dòng chảy.

Điểm quan trọng nhất: kết quả phải TRÙNG bản theo lô `_strip_markdown_for_plain`,
vì hai đường (phát trực tiếp và dự phòng theo lô) cùng chảy vào một bong bóng
chat. Lệch nhau là người xem thấy hai kiểu chữ khác nhau tùy hôm agent có chạy.
"""
import main
from plain_stream import PlainTextStreamer


def run_stream(text: str, size: int) -> str:
    """Đẩy `text` qua bộ gỡ theo từng mẩu `size` ký tự."""
    streamer = PlainTextStreamer()
    out = [streamer.feed(text[i:i + size]) for i in range(0, len(text), size)]
    out.append(streamer.close())
    return "".join(out)


SAMPLES = [
    "Tháng này bạn chi **12.460.000 ₫**, tăng 34% so với tháng trước.",
    "# Tổng quan\n\nChi tiêu đang vượt ngân sách.\n",
    "> Lưu ý: số liệu tính tới hôm nay.\nPhần còn lại giữ nguyên.",
    "Bảng dưới đây:\n| Nhóm | Số tiền |\n| --- | --- |\n| Ăn uống | 4.200.000 |\nHết bảng.",
    "- Ăn uống: 4.200.000 ₫\n- Đi lại: 1.100.000 ₫",
    "Không có dấu hiệu nào cả.",
    "__Đậm kiểu gạch dưới__ và chữ thường.",
    "Trường session_flags và new_device giữ nguyên dấu gạch dưới.",
    "",
]


def test_ket_qua_trung_ban_theo_lo():
    for sample in SAMPLES:
        mong_doi = main._strip_markdown_for_plain(sample)
        for size in (1, 3, 7, 1000):
            thuc_te = run_stream(sample, size).strip()
            assert thuc_te == mong_doi, f"lệch ở mẩu {size} ký tự: {sample!r}"


def test_khong_nuot_dau_gach_duoi_trong_ten_truong():
    # Gỡ mọi dấu _ sẽ biến session_flags thành sessionflags — hỏng cả nội dung
    # kỹ thuật lẫn tên tiếng Việt có gạch dưới.
    assert "session_flags" in run_stream("Trường session_flags đổi kết quả.", 1)


def test_bo_ca_dong_bang_markdown():
    text = "Trước bảng\n| a | b |\n| - | - |\nSau bảng"
    # Bản theo lô bỏ hẳn dòng bảng, không để lại dòng trống — bản dòng chảy phải giống.
    assert run_stream(text, 2) == main._strip_markdown_for_plain(text) == "Trước bảng\nSau bảng"


def test_chu_chay_ra_ngay_khong_cho_het_cau():
    """Đây là lý do tồn tại của lớp này: chữ phải ra khi mới có vài ký tự."""
    streamer = PlainTextStreamer()
    assert streamer.feed("Tháng này bạn ") == "Tháng này bạn "
    assert streamer.feed("chi 12.460.000 ₫") == "chi 12.460.000 ₫"


def test_dau_hieu_bi_cat_giua_hai_mau():
    # "**" rơi vào hai mẩu khác nhau: không được để lọt một dấu * ra màn hình.
    streamer = PlainTextStreamer()
    first = streamer.feed("giá trị *")
    second = streamer.feed("*quan trọng** xong")
    assert "*" not in (first + second + streamer.close())


def test_dong_chi_co_tieu_de_khong_lam_mat_xuong_dong():
    assert run_stream("### Tiêu đề\nNội dung", 4) == "Tiêu đề\nNội dung"
