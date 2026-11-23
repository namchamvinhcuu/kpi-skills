# odoo-kpi-skills — hướng dẫn cho nhân viên QA/QC

Plugin này cho phép bạn nhờ Claude Code dựng **dashboard KPI trong Odoo** từ chính file Excel bạn đã tải lên mục
Documents. Excel giữ nguyên hình thức (màu, ô gộp, độ rộng, định dạng); bạn chỉ nói ô nào lấy dữ liệu nào, Claude làm
phần còn lại và xuất bản dashboard dưới tài khoản của bạn.

Plugin chỉ dùng **nguồn dữ liệu có sẵn** (sản xuất, xuất hàng, lịch sử kiểm tra QMS, số liệu nhập tay theo năm). Nếu KPI
cần dữ liệu chưa có nguồn, Claude sẽ **không tự dựng gì** mà viết sẵn một đoạn yêu cầu để bạn gửi cho nhà phát triển.

> Kho mã này công khai: không bao giờ đưa vào đây file Excel của khách, mật khẩu, API key hay địa chỉ máy chủ thật.

## 1. Cần gì trước khi dùng (khoảng 15 phút, làm một lần)

1. **Claude Code** và **Python 3** (lệnh `python`, hoặc `py` trên Windows) trên máy của bạn. Trên Windows hãy dùng Python tải
   từ python.org (hoặc `winget install Python.Python.3.12`), **không dùng Python cài từ Microsoft Store**: bản đó chuyển
   hướng `%APPDATA%` vào thư mục riêng của nó (chưa kiểm chứng trên máy thật), nên `icacls` không thấy file cấu hình và
   `init-config` báo lỗi về quyền của file. Kiểm tra bản đang dùng bằng `py -0p` hoặc
   `python -c "import sys; print(sys.executable)"`, tránh đường dẫn có `WindowsApps` hoặc `PythonSoftwareFoundation`
   (lệnh `py` có thể chọn bản Store nếu đó là bản mới nhất). Nếu đúng là bản đó và không đọc được quyền file, thông báo lỗi sẽ nói rõ.
2. Một tài khoản Odoo nội bộ của chính bạn. Quản trị viên Odoo đã chạy `setup_instance.py` và đã cho tài khoản của bạn
   quyền tạo dashboard (xem mục 7). Nếu chưa, bước xuất bản sẽ báo lỗi và bạn nhờ quản trị viên.
3. Cài plugin:
   ```
   /plugin marketplace add namchamvinhcuu/kpi-skills
   /plugin install odoo-kpi-skills@odoo-kpi
   ```

## 2. Tạo API key riêng cho máy này (5 phút)

1. Trong Odoo: biểu tượng người dùng > **Preferences** > tab **Account Security** > **New API Key**.
2. Đặt tên có ý nghĩa và **riêng cho từng máy**, ví dụ `kpi-laptop-<tên bạn>-<tháng/năm>`. Nhập lại mật khẩu khi được hỏi.
3. Sao chép key **ngay** (Odoo chỉ hiện một lần) và dán vào file cấu hình ở mục 3. Không gửi key qua chat, email, ảnh chụp.

**Thu hồi (revoke):** Preferences > Account Security > bấm xóa key đó. Làm ngay khi **mất hoặc đổi máy**, khi rời công ty,
hoặc nghi ngờ key bị lộ; sau đó báo quản trị viên Odoo. Mỗi máy một key nên thu hồi máy này không ảnh hưởng máy khác.

## 3. File cấu hình

**Cách dễ nhất:** mở một cửa sổ terminal bình thường của máy (**không** phải trong Claude Code) và chạy
`python <thư mục plugin>/scripts/odoo_rpc.py init-config`. Chương trình hỏi địa chỉ Odoo, tên cơ sở dữ liệu, tên đăng nhập
và API key (gõ không hiện chữ), tự tạo file đúng quyền riêng tư, rồi bạn gõ lại **đầy đủ tên máy chủ** để thử kết nối. Gõ
nhầm API key vào ô khác thì chương trình từ chối và nhắc xóa key đó trong Odoo. Claude không bao giờ tự chạy lệnh này và
không được chạy nó qua terminal giả.

Làm tay thay cho lệnh trên: xem file nằm ở đâu: `python scripts/odoo_rpc.py config-path` (chạy trong thư mục plugin). Nội dung:

```
[odoo]
url = https://<tên-máy-chủ-odoo>
db = <tên-cơ-sở-dữ-liệu>
login = <tên đăng nhập của bạn>
api_key = <API key ở mục 2>
```

Hoặc đặt 4 biến môi trường `ODOO_KPI_URL`, `ODOO_KPI_DB`, `ODOO_KPI_LOGIN`, `ODOO_KPI_API_KEY` (đủ cả bốn).
Dòng `project = <tên>` trong file là tùy chọn: chỉ cần khi plugin có nhiều danh mục dự án (khi đó `init-config` hỏi thêm
một ô "Project"; biến môi trường `ODOO_KPI_PROJECT` nếu có sẽ ghi đè dòng này).

File phải là **của riêng bạn**, các script từ chối đọc nếu không:
- Linux / macOS: `chmod 600 <file>`.
- Windows 11: để file trong thư mục hồ sơ của bạn (mặc định `%APPDATA%\odoo-kpi`). Chỉ bạn, SYSTEM và Administrators được có
  quyền cho phép. Nếu thấy mục khác (Everyone, Users, Domain Users, tài khoản khác), gỡ bằng `icacls`. Nếu máy không đọc
  được quyền của file, script từ chối và bạn dùng 4 biến môi trường ở trên thay cho file.

Kiểm tra: `python scripts/odoo_rpc.py check-config`, sau đó
`python scripts/odoo_rpc.py ping --confirm-instance <tên-máy-chủ>` — gõ **đầy đủ tên máy chủ** (ví dụ `abc.example.com`,
không phải `abc`), để một địa chỉ giả mạo na ná không qua được. **Bạn tự gõ**, Claude luôn hỏi bạn và không tự điền.

## 4. Cách dùng hằng ngày

1. Tải file Excel lên Odoo Documents (nếu chưa có). Mở file trong Documents, dán địa chỉ trên thanh trình duyệt cho Claude
   (con số sau `id=` là mã tài liệu).
2. Nói với Claude, ví dụ: *"Dùng skill kpi-builder: tôi muốn làm KPI từ file này, tab `<tên tab>`, vùng `A1:Q31`; ô D9:O9 là
   số phiếu kiểm tra PQC theo tháng, hàng dưới là cùng kỳ năm trước, hàng dưới nữa là tỷ lệ giữa hai hàng."*
3. Claude tạo một bản sao tạm của file trong Odoo và đưa cho bạn **một địa chỉ**. Bạn mở địa chỉ đó, đợi bảng hiện ra rồi chọn
   menu **File > Snapshot** (không cần sửa gì; tên menu theo ngôn ngữ Odoo của bạn, ví dụ tiếng Hàn là `파일 > 스냅샷`).
   Claude lấy dữ liệu và xóa bản sao tạm.
4. Claude **tự quét file Excel** và đề xuất sẵn bảng "dòng nào lấy nguồn nào" dựa trên nhãn dòng (ví dụ "DK 냉장고" là nhóm
   DK_REF) và tiêu đề khối ("출하 수량" là xuất hàng). Claude cho bạn xem: các dòng đã khớp, các dòng **không khớp** (hỏi bạn,
   hoặc viết yêu cầu cho nhà phát triển), và các **cảnh báo** (ví dụ khối ghi năm trước nhưng tiêu đề tháng ghi năm nay). Bạn chỉ
   cần sửa chỗ sai và trả lời phần còn thiếu. Nhãn mà catalog chưa biết có thể thêm vào file `my_aliases.json` của riêng bạn
   (trong thư mục làm việc của bạn, không phải thư mục plugin).
5. Claude dựng dashboard rồi **đối chiếu số**: so các số bạn (hoặc designer) đã gõ tay trong Excel với số thật trong Odoo, và
   báo một dòng tóm tắt kiểu `replaced 168 cells: 153 typed (84 equal, 0 equal after rounding, 69 differ, 0 error), ...` (ví dụ)
   kèm danh sách ô lệch (`Excel 11, live 10`). Ô lệch **không phải lỗi**: bạn quyết định bên
   nào đúng (ví dụ số năm ngoái gõ tay so với số năm nay). Nếu có ô báo `error`, Claude dừng và không xuất bản.
6. Claude cho bạn xem **kế hoạch xuất bản** (tên `<tên> v1`, nhóm "KPI (QA/QC)"). Bạn đồng ý thì Claude mới xuất bản thật.
7. Mở Odoo > Dashboards > nhóm **KPI (QA/QC)**, xem lại vài ô mà bước đối chiếu không so được (ô trống trong Excel). Muốn sửa:
   nói với Claude, Claude xuất bản **phiên bản mới** (v2, v3) nên các chỉnh sửa tay trước đó không bị mất.

**Tháng sau, hoặc khi khách gửi Excel mới:** Claude tìm lại file spec cũ trong thư mục làm việc (`<tên KPI>.spec.json`, tên do
Claude tự đặt từ lời bạn, chỉ gồm chữ, số, `_` và `-`, không lấy nguyên từ tên file Excel) thay vì hỏi lại từ đầu. Claude quét lại
Excel mới, **so với spec cũ (kể cả chữ ở các ô nhãn và ô nào có số hay để trống) và cho bạn xem chỗ nào giống, chỗ nào đã dịch
hoặc mới**; chỉ khi bạn đồng ý, và lời đồng ý nêu rõ các ô đã dịch, mới làm mới spec. Nếu một dòng đã dịch chỗ, Claude báo
`label shift` và nói rõ dòng nào để bạn quyết định. Chạy quét lại sau khi thêm nhãn mới vào `my_aliases.json` thì Claude dùng
`--force` (file nháp đã có) và không chỉ định `--out`; quét từ chối ghi đè file nhãn, file Excel đã đọc và mọi file `.spec.json`.
Tên tab Excel hoặc tên dashboard có ký tự đặc biệt (dấu nháy, `$`, `&`, `;`, `|`, `(`, `)`, `%`, `^`, `!`, dấu gạch ngược...) thì
Claude không đưa vào lệnh mà nhờ bạn đổi tên tab hoặc gõ lại tên dashboard.

**Lệnh nhanh:** `/odoo-kpi-skills:kpi <địa chỉ Excel trong Odoo Documents> [đường dẫn file design]` bắt đầu cả quy trình trên.

Hai bước mới do hai script chạy phía sau (Claude gọi, bạn không phải gõ): `scripts/scan_excel.py` (bước 4, quét Excel và đề
xuất bảng, chạy không cần mạng) và `scripts/reconcile_kpi.py` (bước 5, đối chiếu số, chỉ đọc dữ liệu).

**Có file design đi kèm (pptx, PDF, ghi chú của designer):** file Excel của khách vẫn là bắt buộc, vì chỉ Excel có địa chỉ
ô. File design chỉ giúp Claude hiểu ý (công thức, dòng nào là nhóm nào). Đưa cho Claude đường dẫn file design (PDF đọc tốt
nhất; pptx thì đưa PDF xuất từ pptx hoặc dán đoạn chữ cần thiết). Claude sẽ đề xuất sẵn bảng "ô nào lấy nguồn nào" kèm
số trang, và nêu các chỗ file design và Excel **không khớp nhau** để bạn (hoặc designer) quyết định, rồi mới hỏi phần còn lại.

Những gì bạn sẽ thấy: tiêu đề tháng gõ tay trong Excel (ví dụ `26년 1월`) không đổi theo bộ lọc năm, ô để trống trong Excel
nay hiện `0`, `-` cho giá trị 0 (nếu chọn), `N/A` cho tỷ lệ không có mẫu số, và với số liệu QMS (dạng pivot) tất cả
các tháng hiện 0 khi **năm đang chọn không có dữ liệu** (kể cả ô năm trước).

## 5. Giới hạn cần biết

- Số liệu của khách (file Excel, số in ra khi đối chiếu) nằm trong phiên làm việc với Claude, tức đi qua nhà cung cấp mô hình AI:
  chỉ dùng cho dữ liệu mà công ty cho phép.
- Chỉ dùng các nguồn trong skill `kpi-source-catalog` (Claude đọc từ `catalogs/<project>.json`).
- Không dựng view/model mới, không sửa dashboard có sẵn (KPI 1-4) hay dashboard của đồng nghiệp.
- Excel có biểu đồ, bộ lọc, ô gộp cắt ngang vùng, công thức đọc ra ngoài vùng: Claude dừng và giải thích thay vì cắt bớt âm thầm.
- Một ô đã được nối dữ liệu mà công thức khác của Excel còn đọc trực tiếp (ví dụ cột tổng) thì không dùng được kiểu hiện `-`:
  Claude sẽ đề nghị hiện `0`.

## 6. Lỗi thường gặp

| Thông báo | Nghĩa là | Cách xử lý |
|---|---|---|
| `HTTP 403 ... error 1010` | Cloudflare chặn chữ ký của chương trình | Nhờ chủ máy chủ cho phép |
| `... is not a local dev host: ask the user to type the full host name` | Cần gõ tên máy chủ để xác nhận | Gõ đầy đủ tên máy chủ (ví dụ `abc.example.com`), Claude dùng đúng cái bạn gõ |
| `no snapshot after 600 seconds` | Bạn chưa chọn File > Snapshot trong địa chỉ Claude đưa | Chạy lại, làm đúng bước 3 |
| `label shift` | Nhãn một hàng không còn đúng chỗ (Excel đã đổi) | Chạy lại bước nhận Excel, kiểm tra lại các ô |
| `typed numbers left outside the bindings` | Còn số gõ tay trong vùng chưa nối dữ liệu | Quyết định: nối dữ liệu hay giữ nguyên |
| `the dashboard file is not what the spec produces` | File dashboard không phải do spec và Excel này tạo ra | Bảo Claude dựng lại (bind) rồi dùng đúng file mới cho cả đối chiếu và xuất bản |
| `your Odoo user has no time zone` hoặc `the time zone ... is not one the server knows` | Cần múi giờ để chia tháng cho số liệu QMS | Đưa múi giờ của bạn (ví dụ `Asia/Ho_Chi_Minh`, như trong Preferences của Odoo) |
| `no header row with the 12 months in order was found` | Claude không thấy hàng tiêu đề 12 tháng | Chỉ cho Claude các ô tháng nằm ở đâu |
| `has typed numbers but no catalog source has an alias for it` | Nhãn dòng này catalog chưa biết | Hỏi lại, thêm nhãn vào `my_aliases.json` của bạn, hoặc viết yêu cầu cho nhà phát triển |
| `the baseline changed since the spec was written (sha256 differs)` | Excel đã được đọc lại, spec cũ ứng với bản trước | Claude xem phần đã đổi rồi làm mới spec (mục "tháng sau") |
| `init-config is for you to run in your own terminal` hoặc `needs a terminal` | Lệnh tạo cấu hình chỉ chạy trong terminal của chính bạn | Mở terminal bình thường của máy và chạy lệnh ở mục 3 |
| `looks like an API key` (khi chạy `init-config`) | Bạn gõ nhầm API key vào ô địa chỉ, tên cơ sở dữ liệu, tên đăng nhập hoặc máy chủ | Xóa key đó trong Odoo (Preferences > Account Security), tạo key mới, chạy lại |
| `several source catalogs are installed: pass --project <name>` hoặc `that project is not one of the installed source catalogs` | Plugin có nhiều danh mục nguồn (mỗi dự án một danh mục) và chưa biết bạn thuộc dự án nào | Cho Claude biết tên dự án của bạn (chọn trong danh sách Claude in ra) và thêm dòng `project = <tên>` vào file cấu hình |
| `project: invalid (installed: ...)` hoặc `catalogs: unreadable` (khi chạy `check-config`) | Tên dự án trong cấu hình không có trong plugin, hoặc thư mục plugin bị hỏng | Sửa dòng `project =` cho đúng một tên trong danh sách, hoặc cài lại plugin |
| Một ô hiện lỗi thay vì 0 | Tài khoản của bạn không đọc được nguồn đó | Nhờ quản trị viên cấp quyền |

## 7. Dành cho quản trị viên Odoo (làm một lần cho mỗi máy chủ)

**Chạy `setup_instance.py` trong cửa sổ terminal của chính bạn, không chạy qua Claude Code.** Không `export` các biến
`ODOO_KPI_ADMIN_*` ở terminal đang chạy Claude Code (agent sẽ đọc được và có thể chạy lệnh ghi thay bạn); để script tự hỏi
mật khẩu (không hiện trên màn hình). Skill `kpi-builder` không bao giờ chạy script này.

Dùng tài khoản **quản trị** (không bao giờ dùng file cấu hình của QA/QC). Thông tin đăng nhập lấy từ biến môi trường
`ODOO_KPI_ADMIN_URL`, `ODOO_KPI_ADMIN_DB`, `ODOO_KPI_ADMIN_LOGIN`, `ODOO_KPI_ADMIN_SECRET` (mật khẩu hoặc API key) hoặc hỏi
khi chạy; không ghi ra đâu cả. Tài khoản phải là quản trị hệ thống **và** quản lý dashboard, nếu không script từ chối.

```
python scripts/setup_instance.py status
python scripts/setup_instance.py init --apply                   # tạo nhóm dashboard "KPI (QA/QC)" (chạy lại không sao)
python scripts/setup_instance.py add-user <login> --apply       # cho một QA/QC quyền xuất bản
python scripts/setup_instance.py remove-user <login> --apply    # thu quyền
python scripts/setup_instance.py list-orphans                   # dashboard mà người tạo không còn
python scripts/setup_instance.py remove-group --apply           # chỉ khi nhóm rỗng
```
Không có `--apply` thì chỉ chạy thử. Mỗi thay đổi in một dòng `CHANGE:` — hãy lưu lại. `add-user` / `remove-user` bỏ qua người
đã ở đúng trạng thái (in "no change"); nếu module khác của Odoo tự đổi thêm nhóm khi ghi quyền, mỗi thay đổi phụ cũng được in
thành một dòng `CHANGE: side effect`. Menu bị ẩn (`hide_menu_ids`) của dự án có thể đổi theo: kiểm tra trên máy thử trước. Máy chủ không phải máy cục bộ cần
`--confirm-instance <tên>` do **người** gõ.

**Lưu ý bảo mật (quyết định D2):** quyền xuất bản là quyền *dashboard manager* của Odoo. Người có quyền này **có thể sửa mọi
dashboard và mọi nhóm dashboard**, kể cả KPI có sẵn. Plugin chỉ tự giới hạn: nó chỉ tạo/sửa/lưu trữ/xóa dashboard do chính
người dùng tạo trong nhóm "KPI (QA/QC)". Đó là hàng rào mềm; chỉ cấp quyền cho người tin cậy. Khi một người nghỉ việc, dashboard
của họ không còn "của ai": dùng `list-orphans` để tìm và xóa trong Odoo.

## 8. Dành cho người bảo trì plugin

- Chạy test: `./run-tests.sh` (Linux/macOS) hoặc `run-tests.cmd` (Windows); chỉ cần thư viện chuẩn của Python.
- Trước khi phát hành, kiểm tra danh sách hàm của client không bị lệch so với mã Odoo:
  `KPI_ODOO_ADDONS_PATH=<addons>,<odoo/addons> ./run-tests.sh` hoặc
  `python scripts/validate_formulas.py check-drift --addons-path <addons>,<odoo/addons>`; nếu lệch, chạy `derive` để làm mới
  `scripts/client_functions.json` và xem lại thay đổi.
- Danh mục nguồn `catalogs/<project>.json` chỉ chứa tên model, cột và lưu ý; so với model thật bằng `fields_get` khi nâng cấp Odoo.
