"""Small, dependency-free Turkish/English localisation helpers."""

from __future__ import annotations


SUPPORTED_LANGUAGES = {"tr": "Türkçe", "en": "English"}


ENGLISH: dict[str, str] = {
    # Control panel
    "Sabasakal Mini Ekran 3,5″": "Sabasakal Mini Screen 3.5″",
    "Ayarlar": "Settings",
    "Sistem, oyun, medya ve kablosuz cihaz bilgilerini tek yerden yönetin":
        "Manage system, game, media and wireless device information in one place",
    "Ekran ve donanım": "Display and hardware",
    "Ekran": "Display",
    "Parlaklık": "Brightness",
    "Otomatik": "Automatic",
    "Saat 07.00–19.00 arasında %90, diğer saatlerde %30 parlaklık kullanır.":
        "Uses 90% brightness from 07:00–19:00 and 30% at other times.",
    "07.00–19.00: %90 parlaklık\n19.00–07.00: %30 parlaklık\nOtomatik kapalıyken parlaklığı sürgüden ayarlayabilirsiniz.":
        "07:00–19:00: 90% brightness\n19:00–07:00: 30% brightness\nWhen automatic mode is off, use the slider to adjust brightness.",
    "Ekran yönü": "Screen orientation",
    "Windows açıldığında çalıştır": "Start with Windows",
    "Windows açıldığında uygulamayı ve mini ekranı otomatik başlatır.":
        "Automatically starts the app and mini display when Windows starts.",
    "MSFS 2024 alt bölümünde kullanılacak görünüm.":
        "Layout used in the lower MSFS 2024 panel.",
    "Donanım adları": "Hardware names",
    "En fazla {count} karakter": "Up to {count} characters",
    "İşlemci": "Processor",
    "Ekran kartı": "Graphics card",
    "Bellek": "Memory",
    "Hava": "Weather",
    "{index}. şehir": "City {index}",
    "Hava durumu gösterilecek şehir.": "City whose weather will be shown.",
    "3,5 inç ekran önizlemesi": "3.5-inch display preview",
    "Günlük": "Daily",
    "Kablosuz": "Wireless",
    "Günlük kullanım · medya görünümü": "Daily use · media view",
    "Renkler": "Colours",
    "Hazır palet": "Preset palette",
    "Değiştirmek istediğiniz alanı seçin": "Select the area you want to change",
    "Normal": "Normal",
    "Uyarı": "Warning",
    "Kritik": "Critical",
    "Zemin": "Background",
    "Ana yazı": "Primary text",
    "İkincil yazı": "Secondary text",
    "Renk seçin": "Choose a colour",
    "Seçili renk": "Selected colour",
    "Halka, zemin ve yazı renklerinden birini seçin.":
        "Select a ring, background or text colour.",
    "Paleti kaydet": "Save palette",
    "Geçerli renkleri yeni bir palet olarak kaydeder.":
        "Saves the current colours as a new palette.",
    "Sil": "Delete",
    "Seçili özel renk paletini siler.": "Deletes the selected custom palette.",
    "Kaydet ve başlat": "Save and start",
    "Ayarları kaydeder ve mini ekrana görüntü göndermeye başlar.":
        "Saves settings and starts sending an image to the mini display.",
    "Durdur": "Stop",
    "Veri akışını durdurur ve mini ekranı temizler.":
        "Stops the data stream and clears the mini display.",
    "Yalnızca kaydet": "Save only",
    "Varsayılana dön": "Restore defaults",
    "Renkleri ve görünüm ayarlarını başlangıç değerlerine getirir.":
        "Restores the original colour and appearance settings.",
    "Ekran hazır": "Display ready",
    "Sabasakal Mini Ekran'ı aç": "Open Sabasakal Mini Screen 3.5″",
    "Mini ekranı başlat": "Start mini display",
    "Mini ekranı durdur": "Stop mini display",
    "Çıkış": "Exit",
    "Zowie eklentisi · CS2'de {game}, sonra {desktop}": "Zowie add-on · {game} in CS2, then {desktop}",
    "Zowie monitör": "Zowie monitor",
    # v1.5.1 screen transition
    "Ekran geçişi": "Screen transition",
    "Yumuşak kararma (önerilen)": "Smooth fade (recommended)",
    "Soldan sağa süpürme (eski)": "Left-to-right sweep (old)",
    "Sayfa değişirken ekran ışığı yumuşakça kısılır, yeni görüntü karanlıkta çizilir ve ışık geri açılır.": (
        "When the page changes the backlight fades out, the new image is drawn in the dark and the "
        "light fades back in."
    ),
    # v1.5 network tools
    "Ağ araçları": "Network tools",
    "ExitLag, NoPing gibi programlar kapatıldıktan sonra arka planda servis bırakabilir ve oyunda jitter / paket kaybı yapabilir. Buradan bulup tek tuşla kapatabilirsin.": (
        "Tools such as ExitLag or NoPing can leave a background service running after you close "
        "them, causing in-game jitter or packet loss. Find and stop them here with one click."
    ),
    "Yeniden tara": "Scan again",
    "Hepsini kapat": "Stop all",
    "Yalnızca bu programların süreçlerini ve servislerini kapatır; sürücülere, ağ ayarlarına ve oyuna dokunmaz. Kapattıktan sonra açık olan oyunu yeniden başlatman önerilir.": (
        "Only stops these programs' processes and services; drivers, network settings and the "
        "game are not touched. Restart a running game afterwards."
    ),
    "{name} servisi": "{name} service",
    "Arka planda çalışıyor: {items}": "Running in the background: {items}",
    "Arka planda ağ aracı yok ✓": "No network tools in the background ✓",
    "Arka planda ağ aracı servisi çalışıyor; oyunda paket kaybı yapabilir. Sabasakal > Ağ araçları > Hepsini kapat.": (
        "A network tool service is running in the background and may cause packet loss. "
        "Sabasakal > Network tools > Stop all."
    ),
    "Kapatılıyor…": "Stopping…",
    "Kapatıldı: {items}": "Stopped: {items}",
    "Kapatılamadı: {items}": "Could not stop: {items}",
    "Henüz taranmadı": "Not scanned yet",
    "Ağ araçlarını kapat": "Stop network tools",
    # v1.5 game profiles
    "Oyun profilleri": "Game profiles",
    "Eklediğin oyun açılınca monitör modu, NVIDIA titreşimi, ses çıkışı ve mini ekran ayarlanır; oyun kapanınca hepsi eski haline döner.": (
        "When a game you added starts, the monitor mode, NVIDIA vibrance, audio output and mini "
        "display are set; when it closes everything goes back."
    ),
    "Oyun profillerini kullan": "Use game profiles",
    "Otomatik": "Automatic",
    "Oyunlar": "Games",
    "+ Oyun ekle": "+ Add game",
    "Oyundan çıkınca": "After the game",
    "Monitör modunu değiştirme": "Keep the monitor mode",
    "Henüz oyun eklenmedi": "No games added yet",
    "Düzenle": "Edit",
    "titreşim {value}": "vibrance {value}",
    "ses çıkışı": "audio output",
    "oyun şeridi": "in-game strip",
    "turuncu tema": "orange theme",
    "Oyun profili": "Game profile",
    "Oyun dosyası": "Game file",
    "Gözat…": "Browse…",
    "Görünen ad": "Display name",
    "Değiştirme": "Don't change",
    "NVIDIA titreşimi": "NVIDIA vibrance",
    "Değiştir": "Change",
    "Ses çıkışı": "Audio output",
    "Mini ekran": "Mini display",
    "Turuncu tema (masaüstü ekranı)": "Orange theme (desktop screen)",
    "Oyun şeridi (CPU/GPU/RAM + piller)": "In-game strip (CPU/GPU/RAM + batteries)",
    "Oyun kapanınca titreşim ve ses çıkışı eski haline döner; monitör 'Oyundan çıkınca' ayarındaki moda geçer.": (
        "When the game closes vibrance and audio output go back; the monitor switches to the "
        "'After the game' mode."
    ),
    "Vazgeç": "Cancel",
    "Kaydet": "Save",
    "Oyun şeridi": "In-game strip",
    "Oyun profili · oyun şeridi": "Game profile · in-game strip",
    "Monitör: {mode}": "Monitor: {mode}",
    # v1.4 FACEIT
    "bugün {change}": "today {change}",
    "Son maçlar": "Recent matches",
    "G": "W",
    "M": "L",
    "Son {n} maç ortalaması": "Average of the last {n} matches",
    "FACEIT verisi bekleniyor": "Waiting for FACEIT data",
    "FACEIT ayarlanmadı": "FACEIT is not set up",
    "API anahtarı geçersiz": "Invalid API key",
    "API anahtarının izni yok": "The API key has no permission",
    "FACEIT kullanıcısı bulunamadı": "FACEIT user not found",
    "FACEIT istek sınırı doldu": "FACEIT request limit reached",
    "FACEIT'e bağlanılamadı": "Could not reach FACEIT",
    "FACEIT kartı": "FACEIT card",
    "ELO, seviye ve son maçların; yalnızca FACEIT'in resmi web API'sinden okunur, oyuna dokunulmaz.": (
        "Your ELO, level and recent matches, read only from FACEIT's official web API; the game is never touched."
    ),
    "Kullanıcı adı": "Nickname",
    "FACEIT kullanıcı adın": "Your FACEIT nickname",
    "API anahtarı": "API key",
    "Kayıtlı (değiştirmek için yenisini yapıştır)": "Saved (paste a new one to change it)",
    "Server side API anahtarı": "Server-side API key",
    "Kaydet ve doğrula": "Save and verify",
    "Anahtar nasıl alınır?": "How to get a key?",
    "Durum": "Status",
    "Doğrulanıyor…": "Verifying…",
    "Bağlandı: {name} · Seviye {level} · {elo} ELO": "Connected: {name} · Level {level} · {elo} ELO",
    "Ayarlanmadı": "Not set up",
    "Anahtar Windows'un şifrelemesiyle (DPAPI) yalnızca bu Windows hesabına özel saklanır.": (
        "The key is stored encrypted with Windows DPAPI, readable only by this Windows account."
    ),
    "FACEIT sayfası": "FACEIT page",
    "Boştayken dönen ekran · FACEIT": "Idle carousel · FACEIT",
    # v1.3 carousel pages + electricity
    "Ocak": "January", "Şubat": "February", "Mart": "March", "Nisan": "April",
    "Mayıs": "May", "Haziran": "June", "Temmuz": "July", "Ağustos": "August",
    "Eylül": "September", "Ekim": "October", "Kasım": "November", "Aralık": "December",
    "Pazartesi": "Monday", "Salı": "Tuesday", "Çarşamba": "Wednesday", "Perşembe": "Thursday",
    "Cuma": "Friday", "Cumartesi": "Saturday", "Pazar": "Sunday",
    "Elektrik": "Electricity",
    "Şu an (tahmini)": "Now (estimate)",
    "Bugün": "Today",
    "Bu ay": "This month",
    "fiyat girilmedi": "no price set",
    "CPU + GPU + diğer parçalar · monitör hariç": "CPU + GPU + other parts · monitors excluded",
    "Saat": "Clock",
    "Boştayken dönen ekran · saat": "Idle carousel · clock",
    "Boştayken dönen ekran · hava durumu": "Idle carousel · weather",
    "Boştayken dönen ekran · elektrik": "Idle carousel · electricity",
    "Boştayken": "When idle",
    "Saat, hava, sistem ve elektrik ekranlarını döndür": "Rotate clock, weather, system and electricity screens",
    "Klavye ve fareye bir süre dokunulmayınca mini ekran bu sayfalar arasında döner. Oyun ve uçuş ekranlarını etkilemez.": (
        "When the keyboard and mouse are left alone for a while the mini display cycles through these pages. "
        "Game and flight screens are not affected."
    ),
    "Döndürme": "Rotation",
    "{n} sn dokunulmayınca": "after {n} s idle",
    "{n} dk dokunulmayınca": "after {n} min idle",
    "{n} sn'de bir": "every {n} s",
    "İşlemci ve ekran kartı güç sensörlerinden tahmini tüketim ve maliyet": (
        "Estimated use and cost from the processor and graphics card power sensors"
    ),
    "Fiyat": "Price",
    "Son faturadan hesapla": "Calculate from my last bill",
    "Birim fiyatı kendim gireceğim": "I'll enter the unit price",
    "Son fatura": "Last bill",
    "Toplam tutar": "Total amount",
    "Tüketim": "Usage",
    "Birim fiyat": "Unit price",
    "Örn. 3,24": "e.g. 3.24",
    "Hesaplanan": "Calculated",
    "Tahmin ayarı": "Estimate",
    "W diğer parçalar": "W other parts",
    "% güç kaynağı verimi": "% power supply efficiency",
    "Tahmindir: işlemci ve ekran kartı sensörlerine anakart, bellek, disk ve fanlar için bir pay eklenir ve güç kaynağı verimine bölünür. Monitörler ile uygulamanın kapalı olduğu süre sayılmaz. En doğru fiyat için son faturanızdaki toplam tutarı ve kWh'i girin.": (
        "This is an estimate: an allowance for the motherboard, memory, disks and fans is added to the processor "
        "and graphics card sensors and divided by the power supply efficiency. Monitors and time while the app is "
        "closed are not counted. For the most accurate price enter the total amount and kWh from your last bill."
    ),
    "{price} TL/kWh": "{price} TL/kWh",
    "Fiyat girilmedi": "No price set",
    "Ölçüm uygulama açıldığında başlar": "Measuring starts when the app runs",
    "Şu an {watts}": "Now {watts}",
    "bugün {kwh} kWh": "today {kwh} kWh",
    "bu ay {kwh} kWh": "this month {kwh} kWh",
    "≈ {cost} TL": "≈ {cost} TL",
    # v1.2 sidebar layout
    "Tema": "Theme",
    "Açık": "Light",
    "Koyu": "Dark",
    "Genel bakış": "Overview",
    "Bilgiler": "Information",
    "Hakkında": "About",
    "Mini ekranın parlaklığı, yönü ve tasarımı": "Brightness, orientation and design of the mini display",
    "Başlangıç": "Startup",
    "Kilit ekranı": "Lock screen",
    "Windows kilitlenince mini ekranı kapat": "Turn the mini display off when Windows is locked",
    "Bilgisayar kilitlenince mini ekranın ışığı kapanır; kilit açılınca geri gelir.": (
        "The mini display goes dark while the PC is locked and comes back when you unlock it."
    ),
    "Mini ekranda görünen adlar ve hava durumu şehirleri": "Names and weather cities shown on the mini display",
    "Hava durumu": "Weather",
    "Otomatik mod": "Automatic mode",
    "Monitör modu": "Monitor mode",
    "Sürüm": "Version",
    "Güncellemeler": "Updates",
    "İndirme sayfasını aç": "Open download page",
    "Şimdi denetle": "Check now",
    "Yeni sürümleri otomatik denetle": "Check for new versions automatically",
    "Günde bir kez GitHub'daki sürüm listesine bakar. Hiçbir şeyi kendiliğinden indirmez.": (
        "Looks at the GitHub release list once a day. Never downloads anything by itself."
    ),
    "Denetleniyor…": "Checking…",
    "Denetlenemedi · internet bağlantısını kontrol edin": "Could not check · check your internet connection",
    "Güncel · en son sürüm {version}": "Up to date · latest version {version}",
    "Yeni sürüm var: {version}": "New version available: {version}",
    "Henüz denetlenmedi": "Not checked yet",
    "Windows kilitli · mini ekran kapalı": "Windows locked · mini display off",
    "CS2'de otomatik mod": "Auto mode for CS2",
    "Monitör bulunamadı": "Monitor not found",
    "Şu an: {mode}": "Now: {mode}",
    "Mod okunuyor…": "Reading mode…",
    "CS2 açılınca monitörü oyun moduna, kapanınca masaüstü moduna alır.": (
        "Switches the monitor to the game mode when CS2 starts and back to the desktop mode when it exits."
    ),
    "Dil": "Language",
    # Palette and layout names
    "Kuzey": "North",
    "Mor Gece": "Purple Night",
    "Bakır": "Copper",
    "Buz": "Ice",
    "Tek renk": "Monochrome",
    "Okyanus": "Ocean",
    "Zümrüt": "Emerald",
    "Gece Mavisi": "Midnight Blue",
    "Gün Batımı": "Sunset",
    "Lavanta": "Lavender",
    "Kiraz": "Cherry",
    "Kum Taşı": "Sandstone",
    "Orman": "Forest",
    "Kutup Işığı": "Aurora",
    "Safir": "Sapphire",
    "Mercan": "Coral",
    "Göktaşı": "Meteor",
    "Neon Şehir": "Neon City",
    "Özel": "Custom",
    "Klasik": "Classic",
    "Kokpit şeridi": "Cockpit strip",
    # Colour editor
    "Halkaların normal başlangıç rengi": "Normal starting colour of the rings",
    "Yük ve sıcaklık yükselirken geçiş": "Transition as load and temperature rise",
    "Son eşik ve yanıp sönme uyarısı": "Final threshold and flashing alert",
    "Mini ekranın ana arka planı": "Main mini-display background",
    "Donanım adları, değerler ve ana başlıklar": "Hardware names, values and main headings",
    "Açıklamalar ve ikincil bilgi metinleri": "Descriptions and secondary information",
    "{name} rengi": "{name} colour",
    "Seçili renk: {colour}": "Selected colour: {colour}",
    # Preview context and sample data
    "MSFS 2024 · canlı uçuş bilgileri": "MSFS 2024 · live flight information",
    "Boşta kullanım · seçili şehirlerin hava durumu": "Idle · weather for selected cities",
    "Kablosuz cihazlar · pil durumları": "Wireless devices · battery status",
    "Ekran önizlemesi": "Display preview",
    "Kablosuz Klavye": "Wireless Keyboard",
    "Şu an çalıyor": "Now playing",
    "Müzik ve ritim görünümü": "Music and rhythm view",
    # Dialogs and status messages
    "Renk paletini kaydet": "Save colour palette",
    "Palet adı:": "Palette name:",
    "Paletim": "My palette",
    "Bu ad kullanılamaz": "This name cannot be used",
    "Hazır palet adları değiştirilemez.": "Preset palette names cannot be changed.",
    "Paletin üzerine yazılsın mı?": "Overwrite palette?",
    "{name} adlı palet zaten var.": "A palette named {name} already exists.",
    "Palet silinsin mi?": "Delete palette?",
    "{name} adlı palet silinecek.": "The palette named {name} will be deleted.",
    "{name} paleti kaydedildi": "Palette {name} saved",
    "{name} paleti silindi": "Palette {name} deleted",
    "Varsayılan ayarlara dönüldü": "Default settings restored",
    "Önizleme hazırlanamadı: {error}": "Preview could not be prepared: {error}",
    "{error}\n\nUygulamayı yönetici olarak açıp yeniden deneyin.":
        "{error}\n\nRun the application as administrator and try again.",
    "Ayarlar kaydedilemedi; ekran yine de başlatılıyor · {error}":
        "Settings could not be saved; the display will still start · {error}",
    "Ayarlar kaydedilemedi": "Settings could not be saved",
    "Ayarlar kaydedildi ve çalışan ekrana uygulanacak":
        "Settings saved and will be applied to the running display",
    "Ekran zaten çalışıyor": "Display is already running",
    "Ekran başlatılıyor · ayarları değiştirmek için Durdur":
        "Starting display · select Stop to change settings",
    "Ekran başlatılamadı · {error}": "Display could not be started · {error}",
    "Ekran durduruluyor…": "Stopping display…",
    "Mini ekran bağlandı · görüntü yenileniyor": "Mini display connected · refreshing image",
    "Mini ekran bekleniyor · {error}": "Waiting for mini display · {error}",
    "Bağlantı yenileniyor · {error}": "Refreshing connection · {error}",
    "MSFS 2024 açık · uçuş verileri canlı": "MSFS 2024 running · flight data live",
    "MSFS 2024 açık · kokpit bağlantısı bekleniyor":
        "MSFS 2024 running · waiting for cockpit connection",
    "{label} · ritim görselleştiriliyor": "{label} · visualising audio",
    "Ekran çalışıyor · hava durumu güncel": "Display running · weather up to date",
    "Ekran çalışıyor": "Display running",
    "Bağlantı kurulamadı: {error}": "Connection failed: {error}",
    "Ekran durduruldu": "Display stopped",
    "Uygulama sistem tepsisinde çalışmaya devam ediyor.":
        "The application is still running in the system tray.",
    "Mini ekran bağlantısı bekleniyor. Ayrıntılar için simgeye tıklayın.":
        "Waiting for mini display connection. Click the icon for details.",
    "ayarlar kilitli": "settings locked",
    # Mini display
    "Canlı hız bekleniyor": "Live clock unavailable",
    "sn": "sec",
    "Para": "Money",
    "Sistem sesi": "System audio",
    "Ses oynatılıyor": "Audio playing",
    "Kulaklık": "Headset",
    "Klavye": "Keyboard",
    "Uçuş verisi bekleniyor": "Waiting for flight data",
    "Kokpit açıldığında otomatik bağlanır": "Connects automatically when the cockpit opens",
    "İrtifa": "Altitude",
    "Hava hızı": "Airspeed",
    "Dikey hız": "Vertical speed",
    "İstikamet": "Heading",
    "ft/dk": "ft/min",
    "km/sa": "km/h",
    "kullanımda": "in use",
    "Otomatik başlatma ayarlanamadı · {error}": "Autostart could not be updated · {error}",
    # Design selector
    "Tasarım": "Design",
    "Yeni": "New",
    "Claude: oyuna ve uçuş evresine göre değişen sade ekranlar. Codex: önceki görünüm.":
        "Claude: clean screens that follow the game and flight phase. Codex: previous layout.",
    "Yeni tasarım · masaüstü": "New design · desktop",
    "Yeni tasarım · MSFS seyir": "New design · MSFS cruise",
    # New design screens
    "Fare": "Mouse", "Klv": "Kbd", "Kulak": "Head", "Cihaz": "Device", "Chz": "Dev",
    "açık": "clear", "parçalı bulutlu": "partly cloudy", "sisli": "foggy",
    "karlı": "snowy", "fırtınalı": "stormy", "yağmurlu": "rainy",
    "yük": "load",
    "Maç K/D/A {k} / {d} / {a}": "Match K/D/A {k} / {d} / {a}",
    "YERDE · TAKSİ": "ON GROUND · TAXI", "YERDE · PARK": "ON GROUND · PARKED",
    "Yer hızı": "Ground speed", "kt · sınır 25": "kt · limit 25", "Yakıt": "Fuel",
    "ton": "t", "Ağırlık": "Weight",
    "Park freni AÇIK": "Parking brake ON", "Park freni KAPALI": "Parking brake OFF",
    "Flap {n}": "Flaps {n}", "Strobe AÇIK": "Strobe ON", "Strobe KAPALI": "Strobe OFF",
    "TIRMANIŞ": "CLIMB", "SEYİR": "CRUISE", "Sonraki: {wp} {nm} nm": "Next: {wp} {nm} nm",
    "Kalan {nm} nm": "{nm} nm to go", "Uçuş planı yok": "No flight plan",
    "Rota": "Route", "Varış": "Arrival", "Varış (ETA)": "Arrival (ETA)",
    "Varışta yakıt": "Fuel at arrival", "hesaplanıyor": "calculating",
    "Rezerv üstü": "Above reserve", "Rezerv altı": "Below reserve", "Rüzgar": "Wind",
    "karşıdan": "headwind", "arkadan": "tailwind", "{h} sa {m} dk": "{h} h {m} min",
    "{m} dk": "{m} min", "YAKLAŞMA": "APPROACH", "Sonraki nokta {nm} nm": "Next fix {nm} nm",
    "Hız / Vapp": "Speed / Vapp", "Hız": "Speed", "Alçalma": "Descent", "Yükseklik": "Height",
    "Teker AÇIK": "Gear DOWN", "Teker KAPALI": "Gear UP", "Spoiler KAPALI": "Spoiler OFF",
    "İNİŞ": "LANDING", "Son 5 iniş ort. {v} fpm": "Last 5 landings avg. {v} fpm",
    "Temas anı dikey hız": "Touchdown vertical speed", "YUMUŞAK": "SOFT", "NORMAL": "FIRM",
    "SERT": "HARD", "G kuvveti": "G force", "Zıplama": "Bounce", "Var": "Yes", "Yok": "No",
    "Kayıtlı iniş": "Saved landings", "COM1 aktif / yedek": "COM1 active / standby",
    "NAV1 aktif / yedek": "NAV1 active / standby", "Sonraki nokta": "Next fix",
    # CS2 theme
    "CS2 teması": "CS2 theme",
    "CS2 açıkken · turuncu tema": "While CS2 runs · orange theme",
    "Yeni tasarım · CS2 açıkken turuncu tema": "New design · orange theme while CS2 runs",
    "CS2 açık · ekran CS2 renklerinde": "CS2 running · display in CS2 colours",
}


def normalize_language(value: object) -> str:
    return str(value).lower() if str(value).lower() in SUPPORTED_LANGUAGES else "tr"


def translate(text: str, language: object = "tr", **values: object) -> str:
    template = ENGLISH.get(text, text) if normalize_language(language) == "en" else text
    if not values:
        return template
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template
