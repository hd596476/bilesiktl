"""
BileşikTL günlük veri güncelleyici
==================================
Her iş günü GitHub Actions tarafından çalıştırılır.

Yaptığı iş:
  1. Sembol listesini hazırlar: S&P 500 + S&P 400 (orta ölçek) + Nasdaq-100
     (Wikipedia'dan güncel liste) ve aşağıdaki EK_SEMBOLLER.
  2. Yahoo Finance'ten son 10 yıllık günlük fiyatları indirir (yfinance).
  3. Her sembol için hesaplar:
       p   : son kapanış fiyatı (USD)
       r1, r3, r5, r10 : 1/3/5/10 yıllık yıllık bileşik getiri (temettü dahil)
       v   : yıllık oynaklık (son 5 yılın aylık getirilerinden)
       b   : düzeltilmiş beta (SPY'a göre, son 5 yıl aylık, 0,67*β + 0,33)
  4. ABD 10 yıllık tahvil faizini çeker (risksiz faiz).
  5. Sonucu data/prices.json dosyasına yazar. Site bu dosyayı okur.

Elle çalıştırmak için:
    pip install yfinance pandas lxml requests
    python scripts/update_data.py
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

KOK = Path(__file__).resolve().parent.parent
CIKTI = KOK / "data" / "prices.json"

# Getiri modeli: CAPM
#   Beklenen getiri = Risksiz faiz + Beta × Piyasa risk primi
RISK_PRIMI = 0.045       # hisse senedinin tahvile göre beklenen fazladan getirisi
YEDEK_FAIZ = 0.045       # tahvil faizi alınamazsa kullanılacak değer
BETA_ALT, BETA_UST = 0.3, 2.5

BASLANGIC = (datetime.now(timezone.utc) - pd.DateOffset(years=10, days=25)).strftime("%Y-%m-%d")
PARCA = 150              # tek seferde indirilecek sembol sayısı
EN_AZ_SEMBOL = 300       # bundan az veri gelirse eski dosya korunur

# ---------------------------------------------------------------------------
# Ek semboller: endekslerde olmayan ama Türk yatırımcıların sık baktıkları
# Anahtar = sitede görünen sembol, değer = (Yahoo sembolü, ad, tür)
# ---------------------------------------------------------------------------
EK_SEMBOLLER: dict[str, tuple[str, str, str]] = {
    # ABD ETF'leri
    "SPY": ("SPY", "SPDR S&P 500 ETF", "etf"),
    "VOO": ("VOO", "Vanguard S&P 500 ETF", "etf"),
    "IVV": ("IVV", "iShares Core S&P 500 ETF", "etf"),
    "VTI": ("VTI", "Vanguard Total Stock Market ETF", "etf"),
    "QQQ": ("QQQ", "Invesco QQQ Trust", "etf"),
    "QQQM": ("QQQM", "Invesco Nasdaq 100 ETF", "etf"),
    "VT": ("VT", "Vanguard Total World Stock ETF", "etf"),
    "SCHD": ("SCHD", "Schwab US Dividend Equity ETF", "etf"),
    "VIG": ("VIG", "Vanguard Dividend Appreciation ETF", "etf"),
    "VYM": ("VYM", "Vanguard High Dividend Yield ETF", "etf"),
    "DGRO": ("DGRO", "iShares Core Dividend Growth ETF", "etf"),
    "JEPI": ("JEPI", "JPMorgan Equity Premium Income ETF", "etf"),
    "JEPQ": ("JEPQ", "JPMorgan Nasdaq Equity Premium Income ETF", "etf"),
    "QUAL": ("QUAL", "iShares MSCI USA Quality Factor ETF", "etf"),
    "MTUM": ("MTUM", "iShares MSCI USA Momentum Factor ETF", "etf"),
    "COWZ": ("COWZ", "Pacer US Cash Cows 100 ETF", "etf"),
    "IGV": ("IGV", "iShares Expanded Tech-Software ETF", "etf"),
    "SMH": ("SMH", "VanEck Semiconductor ETF", "etf"),
    "SOXX": ("SOXX", "iShares Semiconductor ETF", "etf"),
    "XLK": ("XLK", "Technology Select Sector SPDR", "etf"),
    "VGT": ("VGT", "Vanguard Information Technology ETF", "etf"),
    "XLF": ("XLF", "Financial Select Sector SPDR", "etf"),
    "XLE": ("XLE", "Energy Select Sector SPDR", "etf"),
    "XLV": ("XLV", "Health Care Select Sector SPDR", "etf"),
    "CIBR": ("CIBR", "First Trust Cybersecurity ETF", "etf"),
    "BOTZ": ("BOTZ", "Global X Robotics & AI ETF", "etf"),
    "ARKK": ("ARKK", "ARK Innovation ETF", "etf"),
    "UFO": ("UFO", "Procure Space ETF", "etf"),
    "ITA": ("ITA", "iShares US Aerospace & Defense ETF", "etf"),
    "VXUS": ("VXUS", "Vanguard Total International Stock ETF", "etf"),
    "VEA": ("VEA", "Vanguard FTSE Developed Markets ETF", "etf"),
    "VWO": ("VWO", "Vanguard FTSE Emerging Markets ETF", "etf"),
    "IWM": ("IWM", "iShares Russell 2000 ETF", "etf"),
    "GLD": ("GLD", "SPDR Gold Shares", "etf"),
    "IAU": ("IAU", "iShares Gold Trust", "etf"),
    "SLV": ("SLV", "iShares Silver Trust", "etf"),
    "TLT": ("TLT", "iShares 20+ Year Treasury Bond ETF", "etf"),
    "BND": ("BND", "Vanguard Total Bond Market ETF", "etf"),
    "IBIT": ("IBIT", "iShares Bitcoin Trust", "etf"),
    # İrlanda merkezli UCITS ETF'ler
    "VWRA": ("VWRA.L", "Vanguard FTSE All-World UCITS (Acc)", "ucits"),
    "VWRL": ("VWRL.L", "Vanguard FTSE All-World UCITS (Dist)", "ucits"),
    "CSPX": ("CSPX.L", "iShares Core S&P 500 UCITS (Acc)", "ucits"),
    "VUAA": ("VUAA.L", "Vanguard S&P 500 UCITS (Acc)", "ucits"),
    "VUSA": ("VUSA.L", "Vanguard S&P 500 UCITS (Dist)", "ucits"),
    "SWDA": ("SWDA.L", "iShares Core MSCI World UCITS (Acc)", "ucits"),
    "IWDA": ("IWDA.AS", "iShares Core MSCI World UCITS (Acc)", "ucits"),
    "SSAC": ("SSAC.L", "iShares MSCI ACWI UCITS (Acc)", "ucits"),
    "EQQQ": ("EQQQ.L", "Invesco EQQQ Nasdaq-100 UCITS", "ucits"),
    "CNDX": ("CNDX.L", "iShares Nasdaq 100 UCITS (Acc)", "ucits"),
    "EIMI": ("EIMI.L", "iShares Core MSCI EM IMI UCITS (Acc)", "ucits"),
    "VFEM": ("VFEM.L", "Vanguard FTSE Emerging Markets UCITS", "ucits"),
    "IUIT": ("IUIT.L", "iShares S&P 500 Info Tech UCITS", "ucits"),
    "SEMI": ("SEMI.L", "iShares MSCI Global Semiconductors UCITS", "ucits"),
    "IUSQ": ("IUSQ.DE", "iShares MSCI ACWI UCITS (Acc)", "ucits"),
    "VHYL": ("VHYL.L", "Vanguard FTSE All-World High Dividend UCITS", "ucits"),
    "SGLN": ("SGLN.L", "iShares Physical Gold ETC", "ucits"),
    # Endeks dışı popüler hisseler (ADR'ler dahil)
    "TSM": ("TSM", "Taiwan Semiconductor (ADR)", "stock"),
    "ASML": ("ASML", "ASML Holding", "stock"),
    "NVO": ("NVO", "Novo Nordisk (ADR)", "stock"),
    "BABA": ("BABA", "Alibaba Group (ADR)", "stock"),
    "SHOP": ("SHOP", "Shopify", "stock"),
    "SAP": ("SAP", "SAP SE (ADR)", "stock"),
    "TM": ("TM", "Toyota Motor (ADR)", "stock"),
    "SONY": ("SONY", "Sony Group (ADR)", "stock"),
    "ARM": ("ARM", "Arm Holdings (ADR)", "stock"),
    "SE": ("SE", "Sea Limited (ADR)", "stock"),
    "MELI": ("MELI", "MercadoLibre", "stock"),
    "SPOT": ("SPOT", "Spotify Technology", "stock"),
    "NU": ("NU", "Nu Holdings", "stock"),
    "SOFI": ("SOFI", "SoFi Technologies", "stock"),
    "HOOD": ("HOOD", "Robinhood Markets", "stock"),
    "RKLB": ("RKLB", "Rocket Lab", "stock"),
    "ASTS": ("ASTS", "AST SpaceMobile", "stock"),
    "IONQ": ("IONQ", "IonQ", "stock"),
    "RBLX": ("RBLX", "Roblox", "stock"),
    "SNOW": ("SNOW", "Snowflake", "stock"),
    "NET": ("NET", "Cloudflare", "stock"),
    "MDB": ("MDB", "MongoDB", "stock"),
    "MNDY": ("MNDY", "monday.com", "stock"),
    "U": ("U", "Unity Software", "stock"),
    "PATH": ("PATH", "UiPath", "stock"),
    "S": ("S", "SentinelOne", "stock"),
    "OKTA": ("OKTA", "Okta", "stock"),
    "ZS": ("ZS", "Zscaler", "stock"),
    "BIDU": ("BIDU", "Baidu (ADR)", "stock"),
    "PDD": ("PDD", "PDD Holdings (ADR)", "stock"),
    "BRK.B": ("BRK-B", "Berkshire Hathaway B", "stock"),
}

# Wikipedia tabloları: (adres, sembol sütunu, ad sütunu)
WIKI = [
    ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", "Security"),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "Symbol", "Security"),
    ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker", "Company"),
]
UA = {"User-Agent": "Mozilla/5.0 (BilesikTL veri guncelleyici)"}


def log(*a):
    print(*a, flush=True)


def wiki_listesi() -> dict[str, tuple[str, str, str]]:
    """Endeks bileşenlerini Wikipedia'dan çeker. Hata olursa boş döner."""
    sonuc: dict[str, tuple[str, str, str]] = {}
    for adres, sym_kol, ad_kol in WIKI:
        try:
            html = requests.get(adres, headers=UA, timeout=30).text
            for tablo in pd.read_html(io.StringIO(html)):
                if sym_kol in tablo.columns and ad_kol in tablo.columns:
                    for sym, ad in zip(tablo[sym_kol], tablo[ad_kol]):
                        sym = str(sym).strip().upper()
                        if not sym or sym == "NAN" or len(sym) > 8:
                            continue
                        sonuc.setdefault(sym, (sym.replace(".", "-"), str(ad).strip(), "stock"))
                    break
            log(f"  {adres.split('/')[-1]}: toplam {len(sonuc)}")
        except Exception as e:  # noqa: BLE001
            log(f"  UYARI: {adres} okunamadı: {e}")
    return sonuc


def onceki_veri() -> dict:
    try:
        return json.loads(CIKTI.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def sembol_evreni() -> dict[str, tuple[str, str, str]]:
    evren = wiki_listesi()
    if len(evren) < 300:
        # Wikipedia'ya ulaşılamazsa önceki dosyadaki listeyi kullan
        eski = onceki_veri().get("symbols", {})
        log(f"  Wikipedia listesi eksik, önceki dosyadan {len(eski)} sembol alınıyor")
        for k, d in eski.items():
            evren.setdefault(k, (d.get("y", k.replace(".", "-")), d.get("n", k), d.get("t", "stock")))
    evren.update(EK_SEMBOLLER)
    return evren


def kur_tablosu() -> dict[str, float]:
    """Yabancı para birimlerinden USD'ye çevirme katsayıları."""
    oranlar = {"USD": 1.0}
    try:
        fx = yf.download(["GBPUSD=X", "EURUSD=X"], period="7d", interval="1d",
                         auto_adjust=False, progress=False, group_by="ticker")
        gbp = float(fx["GBPUSD=X"]["Close"].dropna().iloc[-1])
        eur = float(fx["EURUSD=X"]["Close"].dropna().iloc[-1])
        oranlar.update({"GBP": gbp, "GBp": gbp / 100, "GBX": gbp / 100, "EUR": eur})
    except Exception as e:  # noqa: BLE001
        log(f"  UYARI: döviz kurları alınamadı: {e}")
    return oranlar


def para_birimi(yahoo: str) -> str:
    if "." not in yahoo:
        return "USD"
    try:
        return yf.Ticker(yahoo).fast_info["currency"] or "USD"
    except Exception:  # noqa: BLE001
        return "USD"


def yillik_getiri(seri: pd.Series, yil: int) -> float | None:
    """Son tarihten 'yil' yıl önceye göre yıllık bileşik getiri."""
    son_tarih = seri.index[-1]
    hedef = son_tarih - pd.DateOffset(years=yil)
    if seri.index[0] > hedef + pd.Timedelta(days=10):
        return None  # yeterli geçmiş yok
    bas = seri.iloc[0] if hedef < seri.index[0] else seri.asof(hedef)
    if bas is None or bas <= 0 or math.isnan(bas):
        return None
    return (float(seri.iloc[-1]) / float(bas)) ** (1 / yil) - 1


def oynaklik(seri: pd.Series) -> float | None:
    aylik = seri.resample("ME").last().dropna()
    getiriler = (aylik / aylik.shift(1)).dropna().apply(math.log).tail(60)
    if len(getiriler) < 24:
        return None
    return float(getiriler.std() * math.sqrt(12))


def aylik_getiriler(seri: pd.Series) -> pd.Series:
    aylik = seri.resample("ME").last().dropna()
    return (aylik / aylik.shift(1) - 1).dropna().tail(60)


def beta_hesapla(seri: pd.Series, piyasa: pd.Series | None) -> float | None:
    """Son 5 yılın aylık getirilerinden beta; Bloomberg tarzı düzeltme uygulanır."""
    if piyasa is None:
        return None
    a = pd.concat([aylik_getiriler(seri), piyasa], axis=1, join="inner").dropna()
    if len(a) < 24:
        return None
    var = a.iloc[:, 1].var()
    if not var:
        return None
    ham = a.iloc[:, 0].cov(a.iloc[:, 1]) / var
    duz = 0.67 * ham + 0.33
    return max(BETA_ALT, min(BETA_UST, duz))


def risksiz_faiz() -> float:
    """ABD 10 yıllık tahvil faizi (^TNX)."""
    try:
        t = yf.download("^TNX", period="10d", interval="1d", auto_adjust=False, progress=False)
        deger = float(t["Close"].dropna().iloc[-1].squeeze())
        if deger > 20:          # bazı dönemlerde ×10 olarak yayımlanır
            deger /= 10
        oran = deger / 100
        if 0.002 < oran < 0.12:
            return oran
    except Exception as e:  # noqa: BLE001
        log(f"  UYARI: tahvil faizi alınamadı: {e}")
    return YEDEK_FAIZ


def piyasa_serisi() -> pd.Series | None:
    try:
        d = yf.download("SPY", start=BASLANGIC, interval="1d", auto_adjust=False, progress=False)
        return aylik_getiriler(d["Adj Close"].squeeze().dropna())
    except Exception as e:  # noqa: BLE001
        log(f"  UYARI: SPY verisi alınamadı: {e}")
        return None


def yuvarla(x: float | None, n: int = 4):
    return None if x is None or math.isnan(x) else round(x, n)


def main() -> int:
    log("Sembol listesi hazırlanıyor...")
    evren = sembol_evreni()
    log(f"Toplam {len(evren)} sembol")

    kurlar = kur_tablosu()
    rf = risksiz_faiz()
    piyasa = piyasa_serisi()
    log(f"Risksiz faiz: %{rf * 100:.2f} · SPY verisi: {'var' if piyasa is not None else 'yok'}")
    yahoo_to_key = {v[0]: k for k, v in evren.items()}
    yahoo_list = list(yahoo_to_key)

    semboller: dict[str, dict] = {}
    for i in range(0, len(yahoo_list), PARCA):
        parca = yahoo_list[i:i + PARCA]
        log(f"İndiriliyor {i + 1}-{i + len(parca)} / {len(yahoo_list)}")
        try:
            df = yf.download(parca, start=BASLANGIC, interval="1d", auto_adjust=False,
                             group_by="ticker", threads=True, progress=False)
        except Exception as e:  # noqa: BLE001
            log(f"  UYARI: parça indirilemedi: {e}")
            continue
        for y in parca:
            try:
                d = df[y] if isinstance(df.columns, pd.MultiIndex) else df
                kapanis = d["Close"].dropna()
                duzeltilmis = d["Adj Close"].dropna()
                if len(kapanis) < 120:
                    continue
                key = yahoo_to_key[y]
                ad, tur = evren[key][1], evren[key][2]
                pb = para_birimi(y)
                carpan = kurlar.get(pb)
                if carpan is None:
                    continue
                semboller[key] = {
                    "n": ad,
                    "t": tur,
                    "y": y,
                    "p": yuvarla(float(kapanis.iloc[-1]) * carpan, 2),
                    "r1": yuvarla(yillik_getiri(duzeltilmis, 1)),
                    "r3": yuvarla(yillik_getiri(duzeltilmis, 3)),
                    "r5": yuvarla(yillik_getiri(duzeltilmis, 5)),
                    "r10": yuvarla(yillik_getiri(duzeltilmis, 10)),
                    "v": yuvarla(oynaklik(duzeltilmis)),
                    "b": yuvarla(beta_hesapla(duzeltilmis, piyasa), 3),
                }
            except Exception as e:  # noqa: BLE001
                log(f"  {y} atlandı: {e}")
        time.sleep(1)

    log(f"Başarılı: {len(semboller)} sembol")
    if len(semboller) < EN_AZ_SEMBOL:
        log("HATA: Çok az veri geldi, önceki dosya korunuyor.")
        return 1

    cikti = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "model": {"method": "capm", "rf": round(rf, 4), "erp": RISK_PRIMI},
        "count": len(semboller),
        "symbols": dict(sorted(semboller.items())),
    }
    CIKTI.parent.mkdir(parents=True, exist_ok=True)
    CIKTI.write_text(json.dumps(cikti, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Yazıldı: {CIKTI} ({CIKTI.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
