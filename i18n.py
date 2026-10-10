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
    "Ölüm maçı": "Deathmatch",
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
    "CS2 · ölüm maçı öldürme ve kafa vuruşu görünümü":
        "CS2 · deathmatch kills and headshots view",
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
    "CS2 bağlantısı kurulamadı": "Could not configure CS2 connection",
    "{error}\n\nUygulamayı yönetici olarak açıp yeniden deneyin.":
        "{error}\n\nRun the application as administrator and try again.",
    "CS2 bağlantısı kuruldu.": "CS2 connection configured.",
    " Verilerin gelmesi için CS2'yi bir kez yeniden başlatın.":
        " Restart CS2 once for data to appear.",
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
    "Ölüm maçı · öldürme ve HS verisi canlı": "Deathmatch · kills and HS data live",
    "CS2 maçı açık · canlı veriler izleniyor": "CS2 match active · monitoring live data",
    "FACEIT AC açık · FACEIT pingleri yenileniyor": "FACEIT AC running · refreshing FACEIT pings",
    "CS2 açık · eşleştirme pingleri yenileniyor": "CS2 running · refreshing matchmaking pings",
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
    "HiperZ · arama öncesi": "HiperZ · before queue",
    "Eşleştirme öncesi": "Before matchmaking",
    "Valve rotası": "Valve route",
    "Steam ölçümü hazırlanıyor…": "Preparing Steam measurement…",
    "FACEIT AC açıkken otomatik yenilenir": "Refreshes automatically while FACEIT AC is running",
    "CS2 açıkken otomatik yenilenir": "Refreshes automatically while CS2 is running",
    "Savunma ekonomisi": "Defence economy",
    "Hücum ekonomisi": "Attack economy",
    "Maç ekonomisi": "Match economy",
    "Şimdi": "Now",
    "Kazanırsan": "If you win",
    "Kaybedersen": "If you lose",
    "Kayıp bonusu +${value}": "Loss bonus +${value}",
    "Maç verisi bekleniyor": "Waiting for match data",
    "Ölüm Maçı": "Deathmatch",
    "ÖLDÜRME": "KILLS",
    "KAFA VURUŞU": "HEADSHOTS",
    "{value} kafa vuruşu": "{value} headshots",
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
    "CS2 · CT ekonomi görünümü": "CS2 · CT economy view",
    "CS2 · T ekonomi görünümü": "CS2 · T economy view",
    "CS2 açık · ekonomi verisi canlı": "CS2 running · economy data live",
    # Design selector
    "Tasarım": "Design",
    "Yeni": "New",
    "Claude: oyuna ve uçuş evresine göre değişen sade ekranlar. Codex: önceki görünüm.":
        "Claude: clean screens that follow the game and flight phase. Codex: previous layout.",
    "Yeni tasarım · masaüstü": "New design · desktop",
    "Yeni tasarım · CS2 satın alma": "New design · CS2 buy phase",
    "Yeni tasarım · CS2 round sonu": "New design · CS2 round over",
    "Yeni tasarım · FACEIT (sadece sistem)": "New design · FACEIT (system only)",
    "Yeni tasarım · MSFS seyir": "New design · MSFS cruise",
    # New design screens
    "Fare": "Mouse", "Klv": "Kbd", "Kulak": "Head", "Cihaz": "Device", "Chz": "Dev",
    "açık": "clear", "parçalı bulutlu": "partly cloudy", "sisli": "foggy",
    "karlı": "snowy", "fırtınalı": "stormy", "yağmurlu": "rainy",
    "yük": "load",
    "FACEIT · sadece sistem verisi": "FACEIT · system data only",
    "CS2'yi yeniden başlat · oyun verisi için": "Restart CS2 to enable game data",
    "CS2 · Oyun verisi bekleniyor": "CS2 · Waiting for game data",
    "CS2 · Menü": "CS2 · Menu",
    "CS2'yi yeniden başlatın: oyun verisi için yeni GSI ayarı yüklenmeli":
        "Restart CS2: it must load the new GSI config to send game data",
    
    "Maç K/D/A {k} / {d} / {a}": "Match K/D/A {k} / {d} / {a}",
    "BU ROUND": "THIS ROUND", "Hasar": "Damage", "MAÇ K/D": "MATCH K/D",
    "Asist": "Assists", "Skor": "Score",
    "Sınır {limit}°C · son 30 sn ortalaması {avg}°C": "Limit {limit}°C · last 30 s average {avg}°C",
    "Ani ping artışı · ortalama {avg} ms": "Ping spike · average {avg} ms",
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
