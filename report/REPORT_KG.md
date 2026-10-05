# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Đỗ Hoàng Nam Khánh  **MSSV:** 02423  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Số liệu dưới đây lấy từ `ket_qua_benchmark_kg.txt` (ontology tự thiết kế **DrugKG-2**), đối chiếu ontology gợi ý ở `ket_qua_benchmark_kg.hint.txt`. Provider: OpenRouter `openai/gpt-4o-mini` + `openai/text-embedding-3-small`, top_k=3, chunk_size=800, 176 chunk.

## 1. Chi phí (10 điểm)

```
Chat model: openrouter:openai/gpt-4o-mini | Embedding: openrouter:openai/text-embedding-3-small | top_k=3 | chunk_size=800 | chunks=176 | KG: 493 nodes / 892 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112    108.9
graph       196     94618     5092   0.00996    204.3

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       47   0.00013     2.70
graph       0.89   1.83     3232       83   0.00053     3.08
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.00996 | ×8.9 |
| Indexing giây | 108.9 | 204.3 | ×1.88 |
| Mỗi câu: USD | 0.00013 | 0.00053 | ×4.1 |
| Mỗi câu: giây | 2.70 | 3.08 | ×1.14 |
| Mỗi câu: in_tok | 694 | 3232 | ×4.66 |

**Chi phí tăng thêm đến từ đâu?** Chi phí dựng tăng ×8.9 chủ yếu vì GraphRAG phải gọi LLM **20 lần** để trích xuất 20 bài báo (Flat chỉ embed). Chi phí mỗi câu tăng ×4.1 vì prompt GraphRAG dài hơn ×4.7 token — phần lớn là các dòng dữ kiện graph (khoản luật + khung hình phạt). Độ trễ gần như không tăng (×1.14) vì chi phí đến từ prompt dài, không thêm vòng gọi LLM.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Đáp án nằm gọn trong 1 khoản của Luật PCMT, Flat đã đủ; graph còn chỉ rõ "Điều 2, khoản 4". |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên bị cáo + án tử hình nằm trong 1 bài, Flat lấy được; graph thêm tội danh/Điều 251. |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | **Graph** | "36 tháng tù" ở tin + "Điều 251, 02–07 năm" ở luật; Flat không có đoạn nào chứa cả hai. |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 2 | **Graph** | Hành vi (bắt) ở tin, khung tối đa (khoản 4 Điều 255, chung thân) ở luật; Flat bó tay. |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | **Graph** | Graph mô hình hóa ngưỡng khối lượng nên chọn đúng khoản 4 Điều 250; Flat đoán "khoản b)". |
| Q6 | aggregation | 0.00 / 1 | 0.33 / 1 | **Graph** | Phải gom nhiều vụ theo cùng chất MDMA; Graph liệt kê đúng theo node `Substance`, Flat bỏ sót. |

**Quy luật:** câu trả lời nằm trong **một** nguồn → hai pipeline hòa; câu cần ghép **tin + luật** hoặc **gộp nhiều vụ** → Graph thắng rõ.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E1: Cầu nối gãy — vụ án không nối được sang luật

- **Hiện tượng:** có `Case` không có cạnh `CHARGED_WITH`, nên không đi được sang `Crime`/`Article`.
- **Bằng chứng:**

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name AS name, k.doc_id AS doc_id
```

```
{'name': 'Vụ tông cảnh sát giao thông ở An Giang', 'doc_id': 'news-100260926112415229'}
```

Mở bài báo gốc `news-100260926112415229.md`: bị can bị khởi tố về **"chống người thi hành công vụ"**, không phải tội ma túy (dù lái xe dùng ma túy).

- **Nguyên nhân:** nằm ở **thiết kế ontology + phạm vi corpus**: danh sách tội danh chuẩn chỉ lấy từ Chương XX BLHS (các tội phạm về ma túy). `link_entity` đúng khi trả `None` cho "chống người thi hành công vụ", nhưng ontology thiếu chỗ cho tội **ngoài phạm vi** — vụ có liên quan ma túy lại không nối được, nên câu hỏi kiểu "người này dùng ma túy gì" sẽ hụt ngữ cảnh luật.
- **Đề xuất sửa:** thêm label `OtherCharge` (hoặc `Offence`) cho mọi tội danh không map được, vẫn lưu `charge_text` thô và nối `Case-[:CHARGED_WITH]->OtherCharge`. Đánh đổi: thêm ~1 loại node, prompt trả lời phải phân biệt "tội ma túy" vs "tội khác"; không đủ thì giữ `Case` không có cạnh như hiện tại và ghi chú trong context.

### Lỗi E4: Phép đo `recall` sai — câu trả lời đúng mà điểm thấp

- **Hiện tượng:** Q6 GraphRAG `judge=1` nhưng `recall=0.33`; trong khi Flat `recall=0.00` lại `judge=1`.
- **Bằng chứng (trích `ket_qua_benchmark_kg.txt`):**

```
--- Q6 [aggregation] graph recall=0.33 judge=1
Các vụ việc trong tin tức có liên quan đến ma túy MDMA bao gồm:
1. **Vụ vận chuyển ma túy từ Đức về Việt Nam** - liên quan đến việc vận chuyển 9,6 kg MDMA.
2. **Vụ tổ chức sử dụng ma túy tại Sầm Sơn** - ...
3. **Vụ án tại Viện Pháp y tâm thần Trung ương** - ...
4. **Vụ bắt giang hồ 'Hoàng Nato' ...** - ...
```

`must_include` của Q6 là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`. Câu trả lời nêu đúng **vụ** (chứa "Pháp y tâm thần") nhưng gọi bằng **tên vụ** thay vì tên người, nên chỉ khớp 1/3 từ khóa.

- **Nguyên nhân:** nằm ở **phép đo** (keyword recall) chứ không phải pipeline: nó so khớp chuỗi thô, không tính quan hệ "Cái Quang Huy" ⟷ "Vụ vận chuyển ma túy từ Đức về Việt Nam". Ngược lại Flat liệt kê tên người ("Đức", "Thành", "Đông") nên trông "đúng" hơn dù thiếu vụ Pháp y.
- **Đề xuất sửa:** đổi `must_include` sang tập hợp chấp nhận **tên người hoặc tên vụ**, hoặc tính recall theo alias lấy từ graph (`Person.aliases` + `Case.name`). Đánh đổi: cần bảo trì danh sách alias/đáp án; nhưng phản ánh đúng hơn năng lực GraphRAG.

### Lỗi E6 (phụ): Thuộc tính rác do LLM — `Person` tên "chuỗi rỗng"

- **Hiện tượng:** tồn tại node `Person` với tên là chuỗi placeholder.
- **Bằng chứng:**

```cypher
MATCH (p:Person) WHERE size(trim(p.name)) < 4 RETURN p.name AS name
```

```
{'name': 'chuỗi rỗng'}
```

- **Nguyên nhân:** prompt yêu cầu "chuỗi rỗng nếu không rõ" cho một số trường; LLM đôi khi **chép nguyên hướng dẫn** thành giá trị `name`. Code đã lọc `if p.get("name")` nhưng "chuỗi rỗng" là chuỗi khác rỗng nên lọt qua.
- **Đề xuất sửa:** trong `extract_news_cases`, loại bỏ tên nằm trong danh sách "sentinel" (`{"", "chuỗi rỗng", "không rõ", "n/a"}`) và tên < 3 ký tự. Đánh đổi: thêm một bước lọc hằng số, không tốn token.

## 4. Kết luận (5 điểm)

Với bộ dữ liệu này (176 chunk, 6 câu, 20 bài báo), **Flat RAG đủ cho câu một nguồn** (Q1, Q2 hòa 1.00/2), nhưng **thua hẳn ở câu cross-KB và tổng hợp**: trung bình Graph recall 0.89 / judge 1.83 so với Flat 0.43 / 1.00; ba câu cross-KB (Q3–Q5) Graph đạt recall 1.00 còn Flat 0.00–0.60. Cái giá là **×8.9 chi phí dựng** và **×4.1 chi phí mỗi câu**. Điểm hòa vốn: chi phí dựng thêm ≈ `0.00996 − 0.00112 = 0.00884 USD`; mỗi câu Graph đắt thêm ≈ `0.00053 − 0.00013 = 0.00040 USD` → chỉ cần **~22 câu hỏi** là đã bù được chi phí dựng, càng nhiều câu cross-KB càng đáng dùng KG. Kết luận: dùng **Flat RAG** khi đáp án nằm gọn một đoạn và số câu ít; dùng **GraphRAG** khi câu hỏi xuyên nhiều nguồn/gộp thực thể và hệ thống phục vụ nhiều câu hỏi (≥ ~20).

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
48 passed in 0.07s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openrouter:openai/gpt-4o-mini | embedding = openrouter:openai/text-embedding-3-small
[OK] KG-2 build_graph: 431 node / 797 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 25 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00087. ...
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: Dương Minh Tuấn (tức "Hoàng Nato").

## Vấn đề gặp phải (không tính điểm)

- OpenCode Zen hết credit (`402 Insufficient funds`) và free tier bị chặn khi gọi API trực tiếp, đồng thời Zen không có endpoint embedding → đã chuyển cả chat + embedding sang OpenRouter.
- Lần chạy benchmark đầu tiên GraphRAG recall thấp hơn Flat do hai lỗi trong `context()`: `seed_facts` chiếm hết 60 fact nên fact luật bị cắt, và `"5 viên"` bị parse thành 5 gam nên chọn nhầm khoản. Đã sửa: ưu tiên fact luật trước seed fact, và chỉ parse khối lượng khi có đơn vị khối lượng. Sau khi sửa, Graph recall 0.89 / judge 1.83.
