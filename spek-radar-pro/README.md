# SPEK RADAR PRO

Borsa İstanbul pay piyasasında olağan dışı hareket hazırlığı gösteren payları taramak için araştırma ve sanal test projesi. Gerçek emir göndermez.

## Tüm BIST pay evrenini tarama

Python 3.10+ kurulu ortamda:

```bash
pip install -r requirements.txt
python -m spek_radar.cli --all-bist --period 2y
```

`--all-bist` seçeneği, çalışma anında KAP'ın resmi Pazarlar sayfasından Yıldız, Ana, Alt ve Yakın İzleme pazarlarındaki pay kodlarını çeker. Fonlar, varantlar, borçlanma araçları ve Piyasa Öncesi İşlem Platformu bu taramaya dahil edilmez. KAP sayfa yapısı değişir veya yeterli sayıda kod doğrulanamazsa program eksik evrenle devam etmek yerine hata verir.

Çıktılar:
- `output/universe_snapshot.csv`: taramada kullanılan KAP sembol/pazar anlık görüntüsü
- `output/scan.csv`: skora göre sıralanmış tarama sonuçları
- `output/errors.csv`: verisi alınamayan semboller ve hata açıklamaları

Sadece belirli hisseleri taramak için:
```bash
python -m spek_radar.cli --symbols DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS --period 2y
```

## Skor neyi ölçüyor?

Günlük OHLCV üzerinden göreli hacim, ATR sıkışması, OBV değişimi, kapanışın günlük aralık içindeki yeri ve XU100'e göre 20 günlük göreceli güç hesaplanır. 0–100 puan bir **araştırma önceliği** puanıdır; manipülasyon tespiti, kesin yükseliş tahmini veya yatırım tavsiyesi değildir. Düşük likidite ve veri kalitesi ayrıca incelenmelidir.

## Veri ve kapsam sınırlamaları

İlk veri adaptörü Yahoo Finance/yfinance günlük verisidir; lisanslı, eşanlı BIST verisi değildir ve her payda veri sağlamayabilir. Tüm evren KAP'tan dinamik çekilir; ancak fiyat verisi alınamayan semboller `errors.csv` dosyasına yazılır. Günlük OHLCV, kurum dağılımı/takas, emir defteri, emir iptal akışı veya gerçek para girişi verisi değildir. Bu alanlar için yetkili/lisanslı veri sağlayıcısı entegrasyonu gerekir.

Bugünkü sembol evrenini geçmişe uygulamak survivorship bias yaratır. Üretim öncesi tarihsel bileşen evreni, işlem maliyeti, kayma, devre kesici/işlem sırası kapanma durumları ve walk-forward test eklenmelidir.

## Geçmiş test

```bash
python -m spek_radar.backtest --symbols DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS --period 5y
```

## Güvenlik

Bu proje gerçek emir göndermez. Kripto botunun ana dalına ve mevcut Render servislerine müdahale etmez.
