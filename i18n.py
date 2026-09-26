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
    "CS2 · CT savunma ve bomba imha görünümü": "CS2 · CT defence and bomb defusal view",
    "CS2 · T hücum ve bombayı koruma görünümü": "CS2 · T attack and bomb defence view",
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
    "CS2 açık · C4 ve ekonomi verisi canlı": "CS2 running · C4 and economy data live",
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
    "CT · C4'ü imha et": "CT · Defuse the C4",
    "T · Bombayı koru": "T · Defend the bomb",
    "C4 kuruldu": "C4 planted",
    "sn": "sec",
    "Çözülüyor": "Defusing",
    "Çözme yetişmez": "Not enough time",
    "CT çözüyor": "CT is defusing",
    "Kitli de yetişmez": "Too late with kit",
    "Kitsiz yetişmez": "Too late without kit",
    "Bombayı koru": "Defend the bomb",
    "Patlamaya kalan": "Time to explosion",
    "Para": "Money",
    "5 sn çözme": "5 sec defuse",
    "10 sn çözme": "10 sec defuse",
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
