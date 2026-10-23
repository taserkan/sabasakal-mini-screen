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
