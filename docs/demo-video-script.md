# CaseLens demo video: 2-minute script

All data is SYNTHETIC. Record at 1920×1080 (Windows: Win+Alt+R starts and stops a Game Bar recording; OBS works
too). Use the live Space URL, sign in with **Try the demo (read-only)**, and warm the Space up first: open it once a
few minutes beforehand so the cold start isn't on camera. Ask each question once before recording, so you know the
answer's shape.

| # | Time | On screen | Narration (roughly) |
| --- | --- | --- | --- |
| 1 | 0:00–0:12 | Landing / sign-in page, SYNTHETIC banner visible | "CaseLens is an evidence-search copilot. You upload a mixed case file and ask questions in plain English, and every answer comes back cited to the exact page, second, frame or table row. Everything here is synthetic." |
| 2 | 0:12–0:22 | Click **Try the demo**; the case opens; Evidence tab shows the 18 files (PDFs incl. a scanned statement, 911 audio, interview, CCTV clips, CSVs) | "This is a fictional homicide case: police reports, a scanned handwritten statement, a 911 call, an interview, CCTV and phone and plate-reader records, all ingested: OCR, speech-to-text with speakers, video tracking, entity extraction." |
| 3 | 0:22–0:50 | Ask tab: type **Who was in the gray sedan on March 14?**; show the elapsed timer; the answer appears | "Let's ask the north-star question. The agent plans, searches documents, video and records, and has to cite every claim. Anything it can't cite gets rejected." |
| 4 | 0:50–1:10 | Point at the claims and their numbered citation chips; the **conflict** card side by side (Kemper: "I was by myself" vs witnesses / video: two men) | "Driver Travis Kemper, passenger his cousin Rory Vance. And it surfaces a contradiction instead of hiding it: Kemper says he was alone; the witness and the CCTV say two men got out." |
| 5 | 1:10–1:35 | Click a PDF citation: the page opens with the highlighted box. Click an audio citation: the interview plays from that second, speaker label shown. Optionally a video or CSV-row citation | "Every chip is clickable: here's the exact region of the report, and here's the moment in the interview." |
| 6 | 1:35–1:48 | Ask **What is the serial number of the handgun used to shoot Marcus Hale?** and show `insufficient evidence` | "And when the evidence doesn't support an answer, it says so instead of guessing." |
| 7 | 1:48–2:00 | Timeline tab (scroll the 22:40–22:50 events), then the entity graph, briefly | "A cited timeline and an entity graph come for free. Every view and query is written to a tamper-evident audit log, and an eval harness gates citation precision in CI." |

Fallbacks: if an answer is slow (free LLM tier), cut the wait in editing. If the Space is asleep, open it, wait for
it to wake, and refresh.
