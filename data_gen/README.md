# data_gen: synthetic case data

**Everything here is SYNTHETIC.** These are two fictional cases set in the fictional city of Port Calder.
Phone numbers are in the fictional 555-01xx range and every plate starts with `SYN`. Every generated file
carries a SYNTHETIC notice. Owner: data-gen agent.

| Case | Slug | Contents |
| --- | --- | --- |
| The Riverside Lot Homicide | `riverside_lot` | 7 people, 3 vehicles, 5 phones, 30 events, 4 planted conflicts, 6 unanswerable questions |
| The Harbor Street Burglary | `harbor_street` | Smaller case. It shares exactly one phone number with Riverside (cross-case linking and tenancy tests) |

The ground truth lives in `caselens_data_gen/story/data/*.json`, validated by `story/models.py`. **It never goes
into the app.** It only feeds the generators and the evals.

## Generate

```
pip install -e "./data_gen[audio,video]"     # extras: piper-tts, opencv
cd data_gen/.voices && python -m piper.download_voices \
    en_US-lessac-medium en_US-ryan-medium en_US-amy-medium en_US-joe-medium en_US-hfc_female-medium
python -m caselens_data_gen                  # all cases, all generators (about 80 s)
python -m caselens_data_gen --case harbor_street --only structured,documents
```

Output goes to `data_gen/output/<case>/` (gitignored):

| Folder | What | Generator |
| --- | --- | --- |
| `csv/` | `call_detail_records.csv` (500 rows), `plate_reader_hits.csv` (300), `evidence_log.csv` (40). Columns match the `call_records` / `plate_reads` / `custody_records` tables, and `row_id` is first. **Line 1 is a `#` comment with the notice, so loaders skip lines starting with `#`.** | `structured` |
| `pdf/` | Incident and supplemental reports and witness statements. `ws_pike.pdf` is an image-only scan (OCR test), and `ws_whitfield.pdf` is the planted inconsistent statement | `documents` |
| `audio/` | `aud_911.wav` (128 s), `aud_interview.wav` (300 s), each with a `.transcript.json` gold transcript | `audio` (Piper TTS, local) |
| `video/` | CCTV clips relabeled from MEVA, each with a `.json` sidecar of track summaries | `video` |
| `manifest.json` | Evidence files to upload, with their sha256 | CLI |
| `answer_key.json` | Story fact id mapped to the exact locator (page+bbox, ms, frame+bbox, table+row). This is the source of required citations for the gold set | CLI |

All output is deterministic: running again produces byte-identical files.

## Video source and license

The CCTV clips are cut from the **MEVA dataset** (Kitware / IARPA DIVA), licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) and recorded with consenting hired actors.
They are fetched from the public bucket `mevadata-public-01` by byte range and pinned by sha256 in
`caselens_data_gen/video/sources.json`. Only facts the footage really shows go into the answer key. Each
sidecar lists the story facts its clip can't show. `vid_05_dashcam` isn't produced because MEVA has no
dashcam footage. Network tests run only with `CASELENS_DATA_GEN_NETWORK=1`.
