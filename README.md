# anon-media — 안온 제작 공장 (무료)

안온 AI 오피스의 제작 담당(Claude)이 `jobs/<id>/job.json`을 올리면 GitHub Actions(`.github/workflows/render.yml`)가
카드 PNG·릴스 MP4·목소리 MP3를 만들어 **GitHub Pages**로 공개한다. 공개 저장소라 Actions 분·Pages 모두 무료.

- 결과 기록: `media` 브랜치 `results/<id>.json` (작업 공간은 `https://raw.githubusercontent.com/pypl4325/anon-media/media/results/<id>.json` 으로 확인)
- 공개 주소(링크팜·쓰레드에 줌): `https://pypl4325.github.io/anon-media/out/<id>/r<rev>/...`
- 같은 job은 `rev`를 올려야 다시 만든다. 21일 지난 결과물은 자동 정리.
- 키·토큰은 저장소 Secrets(`ELEVENLABS_API_KEY`)로만 받는다. 파일·기록에 쓰지 않는다.

## job.json

```json
{"id": "040", "rev": 1, "kind": "render",
 "vars": {"scene1": "https://cdn.creativeclaw.co/.../a.jpg"},
 "cards": [{"name": "01", "file": "card1.html"}, {"name": "02", "html": "<!doctype html>..."}],
 "cardSize": [1080, 1350], "thumbs": true,
 "reel": {"file": "reel.html", "size": [1080, 1920], "fps": 30}}
```
- 카드: HTML 그대로 1080×1350 스크린샷 + 720×900 JPG + 한눈 시트(`cards_sheet.jpg`). `{{key}}`는 `vars`로 채움(남으면 오류).
- 릴스: `window.__timelines`(GSAP, paused)를 프레임마다 seek해 30fps로 찍고, `<audio src data-volume data-start data-duration>` 태그(목소리 1.0·배경음 0.16)를 ffmpeg로 섞는다. 길이는 타임라인 길이(또는 `reel.duration`). 검수용 `reel_sheet.jpg`(6장면).

```json
{"id": "v040", "rev": 1, "kind": "voice",
 "voice": {"voice_name": "안온 진행자", "text": "대본…", "model_id": "eleven_v4",
           "settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.3, "speed": 1.05}}}
```
- 일레븐랩스 with-timestamps → `voice.mp3` + 어절별 시작·끝 시각(`words`) — 자막·장면 시간 잡기용. 모델이 안 되면 eleven_v3 → eleven_multilingual_v2 순으로 대신.
- `voice_name` 대신 `voice_id`를 줘도 된다. 이름으로 주면 대표 계정 목소리 목록에서 찾는다(결과에 voice_id 기록).
- `{"id": "voices", "rev": 1, "kind": "voices"}` → 결과 `voices[]`에 대표 계정 목소리 이름·ID 목록.

## results/<id>.json
`status`(done/error) · `cards[]` · `thumbs[]` · `cardsSheet` · `reel{url,sec,bytes,sheet}` · `voice{url,sec,model,voice_id,words}` · `voices[]` · `warn[]`(못 불러온 이미지 등) · `error` · `pagesBase` · `rawBase`
