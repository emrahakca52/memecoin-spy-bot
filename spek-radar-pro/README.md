# SPEK RADAR PRO — MVP

BIST hisselerinde erken uyarı araştırması ve sanal test için ayrı proje. Gerçek emir göndermez.

## Kapsam
- Günlük OHLCV üzerinden göreli hacim, ATR sıkışması, OBV eğimi, kapanış konumu ve XU100'e göre göreceli güç.
- Açıklanabilir 0–100 araştırma puanı.
- DSTKF, KTLEV, TERA ve PEKGY başlangıç sembolleri.
- Sinyal kapanışta hesaplanır; geçmiş testte varsayımsal giriş sonraki seans açılışıdır.
- 1/3/5/10 işlem günü ileri getiri raporu.
- TradingView Pine Script v6 indikatörü.

## Kurulum
Python 3.10+:
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Günlük tarama
```bash
python -m spek_radar.cli --symbols DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS --period 2y
```
Rapor: `output/scan.csv`.

## Geçmiş test
```bash
python -m spek_radar.backtest --symbols DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS --period 5y
```
Rapor: `output/backtest.csv`.

## Veri sınırlamaları
İlk adaptör Yahoo Finance/yfinance günlük verisidir; lisanslı, eşanlı BIST emir/takas verisi değildir. Aracı kurum dağılımı, MKK saklama, emir defteri ve emir iptal akışı henüz bağlı değildir. Fiyat/hacim verisi tek başına manipülasyonu, tahtacıyı veya belirli bir kurumun algoritmasını kanıtlamaz. Sinyal puanı yatırım tavsiyesi değil, araştırma önceliğidir.

BIST Tüm evreni için doğrulanmış güncel ve tarihsel sembol listesi gerekir. Sadece bugünkü sembol listesini geçmişe uygulamak survivorship bias yaratır. Üretim öncesinde lisanslı veri, işlem maliyeti, kayma ve walk-forward test eklenmelidir.

## Güvenlik
Bu proje gerçek emir göndermez ve kripto botunun dosyalarına dokunmaz. Kendi Git dalında ve `spek-radar-pro/` klasöründe tutulur.
