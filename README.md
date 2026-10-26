# Memecoin Spy Pro — temiz paper-only sürüm

Bu paket, gönderdiğin mevcut GitHub ZIP'indeki kaynak kodlar esas alınarak temizlendi.
Derlenmiş `.pyc` dosyaları ve gereksiz önbellek dosyaları pakete alınmadı.

## Güvenlik ve davranış
- İşlem modu `paper`; gerçek emir gönderme özelliği yok ve `real_trading_enabled` kapalı.
- Render açılışında bot otomatik başlamaz. Önce durum uç noktalarını kontrol et; başlatma yalnızca açık `POST /paper-bot/start` isteğiyle olur.
- Fiyat kontrolü, şüpheli ani sıçramaları bekletir; aşırı stop-loss/take-profit fiyatlarında pozisyonu kapatmak yerine önceki işaretli fiyatı korur.
- Eski işlem geçmişi PostgreSQL'deki `memecoin_paper_state` kaydından yüklenir. Bilinen imkânsız MRBEAST satış kaydı geçmişte tutulur ancak raporlanan kâr/zarardan karantinaya alınır.

## Render
Build command: `pip install -r requirements.txt`
Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
Health check path: `/health`

`DATABASE_URL` ve `BIRDEYE_API_KEY` dahil mevcut Render Environment değişkenlerini koru. Veritabanını silme veya yeniden oluşturma.

## GitHub'a temiz yükleme
1. ZIP'i aç.
2. ZIP içindeki dosyaların **kendilerini** (klasörün kendisini değil) deponun kök dizinine yükle.
3. Depoda yalnızca bu paketteki kaynak/dağıtım dosyalarını bırak. `.pyc` dosyalarını veya `__pycache__` klasörlerini yükleme.
4. Commit et ve Render dağıtımının bitmesini bekle.
5. Botu başlatmadan önce sırayla kontrol et:
   - `GET /health`: `mode=paper`, `real_trading_enabled=false`, `bot.running=false`
   - `GET /paper/status`: persistence bağlantısı ve pozisyon sayısı
   - `GET /paper/trades`: geçmiş işlem kayıtları hâlâ geliyor mu?
6. Geçmiş boşsa ya da `DATABASE_URL` hatası varsa botu başlatma; veritabanı ayarını düzeltmeden işlem yapma.

## Sınırlar
Kaynak kodu/sözdizimi testleri, canlı Birdeye erişimini veya Render veritabanını bu ortamdan doğrulamaz. Paper sonuçları simülasyondur; strateji kârlılık garantisi vermez.
