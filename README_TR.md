# Sabasakal Mini Screen 3.5″

Sürüm 1.5.1

1.5.1 değişiklikleri: Ekran geçişi yenilendi. Büyük ekran değişimlerinde
(sayfa geçişi, oyun modu) soldan sağa görünen süpürme yerine ekran ışığı anında
söner, yeni görüntü karanlıkta çizilir ve ışık hızla geri açılır; toplam ~1 sn.
Boştayken dönen sayfaların altında bir sonraki sayfaya kalan süreyi gösteren
ince bir çizgi var. Eski süpürme efekti Ekran > Ekran geçişi'nden seçilebilir.

1.5.0 değişiklikleri: Oyun profilleri (Zowie eklentisinin yerini alır).
Eklenen her oyun (.exe) için monitör modu (ZOWIE GAMER 1/2/3), NVIDIA dijital
titreşim, varsayılan ses çıkışı ve mini ekran görünümü (turuncu tema ya da
oyun şeridi) ayarlanır; oyun açılınca bir kez uygulanır, kapanınca titreşim ve
ses çıkışı eski haline döner, monitör "Oyundan çıkınca" moduna geçer. Eski
değerler önce diske yazılır; uygulama oyun sırasında kapanır ya da çökerse bir
sonraki açılışta geri yüklenir. Monitör modu değişince mini ekranda 3 saniyelik
bildirim çıkar. 1.1.13–1.4 Zowie ayarı otomatik olarak bir CS2 profiline
dönüştürülür. Algılama yalnızca işlem adıdır; oyuna dokunulmaz. Ağ araçları:
ExitLag, NoPing gibi ping düşürücülerin kapatıldıktan sonra arka planda kalan
süreç ve servislerini bulur, tek tuşla (ya da tepsiden) kapatır; oyun sırasında
böyle bir servis çalışıyorsa uyarır. Sürücülere, ağ ayarlarına ve oyuna dokunmaz.

1.4.0 değişiklikleri: FACEIT kartı: ELO, seviye, bugünkü ELO değişimi, son 5
maç ve bu maçların K/D ve HS% ortalaması FACEIT bölümünde ve dönen ekranlarda
görünür. Yalnızca FACEIT'in resmi Data API'si (open.faceit.com) kullanıcının
kendi "Server side" anahtarıyla okunur; oyunla hiçbir iletişim yoktur ve maç
sırasında hiç istek gönderilmez. Anahtar Windows DPAPI ile yalnızca o Windows
hesabına özel şifreli saklanır. 10 dakikada bir ve CS2 kapandıktan 2 ve 5
dakika sonra güncellenir.

1.3.0 değişiklikleri: Elektrik tahmini: işlemci ve ekran kartı güç
sensörlerine diğer parçalar için bir pay (varsayılan 60 W) eklenir, güç kaynağı
verimine (varsayılan %90) bölünür; günlük ve aylık kWh ile TL tutarı Elektrik
bölümünde ve mini ekranda görünür. Fiyat son faturadan (tutar ÷ kWh) ya da
elle birim fiyatla girilir. Monitörler ve uygulamanın kapalı/uykuda olduğu
süre sayılmaz. Boştayken dönen ekranlar (Claude tasarımı): klavye ve fareye
bir süre (varsayılan 1 dk) dokunulmayınca mini ekran saat, hava, sistem ve
elektrik sayfaları arasında döner; oyun ve uçuş ekranları etkilenmez.

1.2.0 değişiklikleri: Yeni, sade kontrol paneli: solda menü (Genel bakış,
Ekran, Bilgiler, Renkler, Zowie monitör, Hakkında), sağda kaydırılan tek
sayfa, altta sabit "Kaydet ve başlat" çubuğu. Varsayılan tema açık (beyaz
zemin, pembe-sarı geçişler); sol alttan Koyu temaya geçilebilir. Windows kilitlenince mini ekran
kapanır, kilit açılınca geri gelir (Ekran bölümünden kapatılabilir). Hakkında
bölümünde güncelleme kontrolü: günde bir kez GitHub'daki herkese açık sürüm
listesine bakar, yeni sürüm varsa haber verir ve yalnızca bu reponun indirme
sayfasını açar; hiçbir şeyi kendiliğinden indirmez veya çalıştırmaz.

1.1.13 değişiklikleri: Zowie eklentisi. Sistem tepsisinden açılır (varsayılan
kapalı). CS2 (`cs2.exe`) açılınca ZOWIE XL monitör bir kez GAMER 2'ye,
kapanınca bir kez GAMER 3'e geçer; oyun açıkken hiçbir şey zorlanmaz, monitörün
tuşlarıyla GAMER 1/GAMER 2 arasında serbestçe geçilebilir, alt-tab modu
değiştirmez. Monitör DDC/CI (VCP 0xDC) ile sürülür; oyunla hiçbir iletişim
yoktur. Geçiş başına tutamaç tutulmaz; 2000 gerçek geçişte bellek ve tutamaç
sayısı sabit kaldı. Modlar `settings.json` içinde `zowie_game_mode` /
`zowie_desktop_mode` (GAMER1/GAMER2/GAMER3) ile değiştirilebilir.

1.1.12 değişiklikleri: CPU sensör yardımcısı birkaç MB'ta kalıyor. .NET,
çöp toplama eşiğini X3D işlemcilerin 96 MB'lık L3 önbelleğine göre
ayarladığı için yaklaşık 30 dakika hiç temizlik yapılmıyor ve yardımcı ~60
MB'a çıkıyordu; artık 4 MB eşikle çalışıyor, CPU sensörlerini her saniye
yerine dakikada bir buluyor ve LibreHardwareMonitor'ın kullanılmayan 24
saatlik değer geçmişini tutmuyor.

1.1.11 değişiklikleri: saniyede yaklaşık 1 MB büyüyen bellek sızıntısı
giderildi (Windows pil taraması her cihazın WinRT özellik tablosunu
kopyalıyordu; bu yerel bellek sızdırıyordu ve bir günde onlarca GB'a
ulaşabiliyordu); tam Windows cihaz taraması en fazla 10 sn'de bir yapılıyor;
G HUB bağlantıları bellekte birikmiyor; kontrol panelindeki bozuk Türkçe
karakterler düzeltildi (1.1.9'da gui.py yanlış kodlamayla kaydedilmişti).
CS2 oyun verisi özellikleri (Game State Integration, ekonomi, ölüm maçı ve
round ekranları, eşleştirme/FACEIT pingleri, uyarı katmanı, G HUB CS2
profili DPI uyarısı) kaldırıldı. CS2
açıkken normal masaüstü ekranı yalnızca CS2'nin turuncu temasıyla çizilir.

1.1.6 düzeltmeleri: mini ekran çıkarılıp takılınca uygulama yeniden
başlamadan bağlanıyor; sensör/çizim hatası USB oturumunu sıfırlamıyor; ekran
çıkarılmışken durdurmak paneli yeniden açmıyor; MSFS yeniden başlatılınca
sahte iniş kaydı ve eski yakıt/telsiz/rota verisi yok; G HUB hatası pil
listesini dondurmuyor; silinen hava durumu şehirleri hemen kalkıyor; Codex
tasarımında uzun cihaz ve şehir adları sığdırılıyor.

1.1.5 düzeltmeleri: kapatılan kablosuz kulaklık/fare (USB alıcı takılı kalsa
da) birkaç saniye içinde ekrandan kalkıyor; ilk sensör başlatması artık
yeniden başlatma olarak günlüğe yazılmıyor; eski kopyayı gösteren başlangıç
görevi onarılıyor veya siliniyor; `--quit` çalışan uygulamayı düzgünce kapatıyor.

1.1.4 düzeltmeleri: Windows pil taraması artık Windows'un zaman zaman iptal
ettiği 30-60 sn'lik AssociationEndpoint taramasını yapmıyor (E_ABORT günlük
satırları bitti); CPU sensör yardımcısı, oyun yükünde LibreHardwareMonitor
yavaşlasa da önbelleği sabit 1 sn aralıkla yazıyor; Claude tasarımındaki satın
alma (para/FULL BUY/ECO/FORCE) ekranı kaldırıldı (freezetime canlı round
ekranını gösterir); ekran geçişleri kararlı istek bekler (en az 2,5 sn) ve
büyük değişimler soldan sağa silme efektiyle gönderilir.

1.1.3 düzeltmeleri: CPU, GPU ve RAM değerleri Windows Görev Yöneticisi ile
aynı (1 sn sayaçlar, ek ortalama yok, GPU = en yoğun motor türü, RAM
"kullanımda" tek ondalık). CS2 açık ama maç verisi yokken (menü, yükleme)
Claude tasarımı masaüstü yerine sistem bilgisi ve eşleştirme pingleri olan bir
CS2 ekranı gösterir; CS2 eski GSI ayarıyla çalışıyorsa "CS2'yi yeniden başlat"
uyarısı çıkar. Oyun içi performans modu: ses yakalama durur, pil taraması
seyrekleşir, öncelik düşer, sensör yardımcısı anakart SuperIO okumaz. İptal
edilen pil taraması bilinen cihazları gizlemez.

1.1.2 düzeltmeleri: "Kaydet ve başlat" artık seçilen tasarımı, ekran yönünü ve
diğer ayarları ekran başlamadan önce gerçekten kaydediyor (1.1.1'de bir Qt
sinyal argümanı kaydı atlatıyordu). Tasarımların adı artık Claude (varsayılan)
ve Codex; MSFS görünüm seçicisi yalnızca Codex'te görünür, Claude tek şehir
(ilk şehir kutusu) için hava durumu gösterir, Codex üç şehri korur. Ekran yönü
ekran çalışırken de değiştirilebilir.

1.1.1 düzeltmeleri: "Ekran" kartındaki seçiciler artık üst üste binmiyor,
180° ekran yönü LCD'de gerçekten uygulanıyor (iki tasarımda da, USB yeniden
bağlandığında da; döndürme yazılımla yapılır) ve Yeni/Klasik tasarım seçimi
başlatıldığında hemen geçerli oluyor. Testler artık tamamen izole çalışır
(geçici profil, gerçek donanım/zamanlanmış görev yok).

Windows üzerinde 480×320 USB mini ekranlar için hazırlanmış modern bir bilgi
panelidir. İşlemci, harici ekran kartı ve RAM değerlerinin yanında MSFS 2024,
medya, hava durumu ve kablosuz cihaz pil bilgilerini gösterebilir. CS2 açıkken
ekran kendiliğinden CS2'nin turuncu temasına geçer.

Hazır paketi kullanmak için son GitHub sürümündeki
`Sabasakal-Mini-Screen-3.5.zip` dosyasını indirin, normal bir klasöre çıkarın ve
`Sabasakal-Mini-Screen.exe` dosyasını çalıştırın. Dil seçimi sağ üsttedir.
ZIP içinden çıkan `_internal` klasörünü EXE'nin yanında bırakın. Bu taşınabilir
klasör yapısı geçici `_MEI` klasörü oluşturmadığı için açılış ve temizlik
hatalarını önler.

CS2 teması için yalnızca `cs2.exe` işleminin çalışıp çalışmadığına bakılır;
uygulama CS2 ile hiç konuşmaz. Oyun belleğine erişme, kod enjekte etme, giriş
otomasyonu veya paket yakalama yapmaz.

1.1.0 ile gelenler:

- Kontrol panelinden seçilebilen iki ekran tasarımı: **Claude** (varsayılan) ve
  **Codex**.
- Bomba sayacı kaldırıldı.

![Yeni tasarım, CS2 teması](docs/images/new/cs2-theme.png)

Ayrıntılı özellikler, kaynak derleme adımları ve lisans bilgileri için
[İngilizce ana belgeye](README.md) bakabilirsiniz.
