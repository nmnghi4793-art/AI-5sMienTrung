
import random

# Danh sách Vấn đề (mới)
problem_bank = [
    "Cần kiểm tra vệ sinh thường xuyên",
    "Thiếu đồ dùng vệ sinh",
    "Cần bảo trì cửa ra vào",
    "Cần thêm ánh sáng",
    "Bồn cầu bẩn",
    "Tường có vết bẩn",
    "Thiếu vệ sinh định kỳ",
    "Cần sắp xếp lại không gian",
    "Hàng hóa không được sắp xếp gọn gàng",
    "Một số pallet không có nhãn",
    "Khu vực đi lại bị cản trở",
    "Cần vệ sinh thường xuyên hơn",
    "Thiếu quy định về bảo quản hàng hóa",
    "Có nhiều hàng hóa nhưng chưa được sắp xếp gọn gàng",
    "Sàn nhà có bụi bẩn",
    "Một số khu vực chưa được chăm sóc thường xuyên",
    "Bàn làm việc có nhiều thiết bị nhưng chưa sắp xếp gọn gàng",
    "Có hộp carton chưa được xử lý",
    "Không rõ ràng về việc sắp xếp hàng hóa",
    "Cần cải thiện vệ sinh",
    "Thiếu dấu hiệu phân khu rõ ràng",
    "Cần cải thiện vệ sinh khu vực làm việc",
    "Cần sắp xếp dây điện gọn gàng hơn",
    "Bàn làm việc có nhiều thiết bị nhưng chưa được tổ chức tốt",
    "Cần vệ sinh bề mặt bàn thường xuyên",
    "Hàng hóa chưa được sắp xếp gọn gàng",
    "Một số pallet không đồng nhất",
    "Hàng hóa không được sắp xếp gọn gàng",
    "Một số pallet có hàng hóa chất đống",
    "Cần cải thiện vệ sinh khu vực",
    "Thiếu nhãn mác cho hàng hóa",
    "Không có lối đi rõ ràng giữa các khu vực",
    "Bụi bẩn trên sàn",
    "Không có khu vực phân loại rõ ràng",
    "Một số hàng hóa chưa được sắp xếp gọn gàng",
    "Thiếu nhãn mác cho một số hàng hóa",
    "Không gian di chuyển hạn chế"
]

# Danh sách Khuyến nghị (mới)
solution_bank = [
    "Thêm giấy vệ sinh",
    "Bảo trì thiết bị vệ sinh",
    "Lắp đèn chiếu sáng tốt hơn",
    "Duy trì lịch vệ sinh",
    "Sắp xếp lại không gian để thoáng hơn",
    "Sắp xếp hàng hóa theo loại",
    "Gắn nhãn cho tất cả pallet",
    "Dọn dẹp khu vực đi lại",
    "Thực hiện vệ sinh định kỳ",
    "Đào tạo nhân viên về quy trình bảo quản hàng hóa",
    "Sắp xếp hàng hóa theo khu vực rõ ràng",
    "Dọn dẹp bụi bẩn trên sàn",
    "Thực hiện kiểm tra định kỳ về vệ sinh",
    "Đảm bảo có quy trình bảo trì cho khu vực",
    "Tăng cường kỷ luật trong việc giữ gìn vệ sinh",
    "Sắp xếp lại thiết bị trên bàn",
    "Xử lý hộp carton",
    "Duy trì vệ sinh thường xuyên",
    "Tạo không gian làm việc thoải mái hơn",
    "Đảm bảo có đủ dụng cụ cần thiết",
    "Vệ sinh bồn cầu thường xuyên",
    "Kiểm tra và sửa chữa các vết bẩn trên tường",
    "Đặt lịch vệ sinh định kỳ",
    "Sử dụng kệ để đồ để giảm bừa bộn trên bàn",
    "Tổ chức dây điện bằng cách sử dụng băng dính hoặc ống bảo vệ",
    "Đặt lịch vệ sinh định kỳ cho khu vực làm việc",
    "Tổ chức lại hàng hóa",
    "Thêm biển chỉ dẫn",
    "Đào tạo nhân viên về 5S",
    "Sắp xếp hàng hóa theo loại và kích thước",
    "Đảm bảo pallet được xếp gọn gàng",
    "Dọn dẹp vệ sinh thường xuyên",
    "Tạo lối đi rõ ràng giữa các khu vực",
    "Đào tạo nhân viên về quy tắc 5S",
    "Sử dụng nhãn mác rõ ràng cho hàng hóa",
    "Tối ưu hóa không gian di chuyển",
    "Kiểm tra định kỳ tình trạng hàng hóa"
]

def get_random_problems(n=5):
    return random.sample(problem_bank, min(n, len(problem_bank)))

def get_random_solutions(n=5):
    return random.sample(solution_bank, min(n, len(solution_bank)))

if __name__ == "__main__":
    print("⚠️ Vấn đề:")
    for p in get_random_problems():
        print(" •", p)
    print("\n🛠️ Khuyến nghị:")
    for s in get_random_solutions():
        print(" •", s)
