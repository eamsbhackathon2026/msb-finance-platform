-- =====================================================================
-- Chuẩn bị demo: đặt sổ tiết kiệm 7009 (khách 100008, 770tr) đáo hạn ĐÚNG
-- HÔM NAY (giờ VN), để endpoint /api/invest/maturing-deposits và thông báo
-- "sổ đến hạn hôm nay" trên màn Copilot trả dữ liệu THẬT từ database.
--
-- Chạy TRƯỚC buổi demo (chạy lại bao nhiêu lần cũng được):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/demo_deposit_due_today.sql
--
-- Không nằm trong seed vì "hôm nay" là ngày động — seed tĩnh sẽ sai ngay
-- ngày hôm sau. Muốn trả sổ về trạng thái gốc: set maturity_date = '20270410'.
-- =====================================================================

UPDATE deposit
SET maturity_date = to_char((now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date, 'YYYYMMDD')
WHERE deposit_id = 7009;
