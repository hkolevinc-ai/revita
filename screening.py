"""Conservative content screening. This is a pre-screen, not a Temu approval."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image, ImageOps


def textnorm(value):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(value or '')).replace('\u00ad','').replace('’',"'")).strip()


# These lists intentionally include some health-benefit wording that may be
# lawful with substantiation. The requested export takes the conservative route.
MEDICAL = {
 'disease':r'\b(?:cancer|tumou?r\w*|diabet\w*|arthritis|arthrosis|osteoarthritis|osteoporosis|hypertension|alzheimer\w*|dementia|depression|anxiety|insomnia|eczema|psoriasis|herpes|infection\w*|virus\w*|viral|antiviral|antibacterial|antibiotic\w*|parkinson\w*)\b|(?:рак(?:ови|а|ът)?\b|тумор\w*|диабет\w*|артрит\w*|артроз\w*|остеопороз\w*|хипертони\w*|алцхаймер\w*|деменци\w*|депреси\w*|тревожност\w*|безсъни\w*|екзем\w*|псориаз\w*|херпес\w*|инфекци\w*|антивирус\w*|антибактери\w*|антибиот\w*)',
 'treatment':r'\b(?:cures?|treats?|treating|treatment|prevents?|prevention|heals?|healing|therapeutic|medicinal|pain\s+relief|relieves?\s+pain|anti[ -]?inflammator\w*)\b|(?:лекува\w*|лечение\w*|лечеб\w*|терапевтич\w*|обезбол\w*|противовъзпал\w*|предпазва\s+от|превенци\w*|профилакти\w*)',
 'metabolic_or_weight':r'\b(?:blood\s+sugar|blood\s+pressure|cholesterol|detox\w*|weight\s*loss|fat\s*burn\w*|appetite\s*suppress\w*|erectile|testosterone\s*boost\w*|anti[ -]?aging)\b|(?:кръвн\w*\s+(?:захар|наляган)|холестерол\w*|детокс\w*|отслаб\w*|изгаря\w*\s+мазнин|потиска\w*\s+апетит|еректил\w*|увеличава\w*\s+тестостерон|антистаре\w*)',
 'health_benefit':r'\b(?:support\w*|boosts?|improves?|enhances?|strengthens?|relieves?|reduces?\s+stress|immune\s+(?:health|support|defen[cs]e)|brain\s+health|heart\s+health|joint\s+health|prostate\s+health|well[ -]?being|wellness|traditional\s+tonic|cognitive|memory|focus|calm|anti[ -]?stress|stress\s+relief)\b|(?:подпомага\w*|подобрява\w*|подкрепя\w*|поддържа\w*|нормализира\w*|засилва\w*|стимулира\w*|облекчава\w*|регулира\w*|имун\w*|\bпамет\w*|\bсъня?\b|здрав\w*\s+(?:сърц|став|кост|простата|мозък))'
}
ECO = {
 'environmental':r'\b(?:eco[ -]?friendly|environmentally\s*friendly|ecological|sustainab\w*|biodegrad\w*|compostab\w*|recycl\w*|carbon\s*(?:neutral|negative|free)|climate\s*(?:neutral|positive)|planet\s*friendly|earth\s*friendly|zero\s*waste|green\s*(?:product|choice|packaging|certifi\w*))\b|(?:екологич\w*|еко\s*продукт\w*|еко\s*сертифи\w*|устойчив\w*\s+(?:продукт|опаков|производ)|биоразград\w*|компостир\w*|рецикли\w*|въглерод\w*\s+неутрал\w*|нулев\w*\s+отпад)',
 'organic_and_certification':r'\b(?:organic|bio|biodynamic|usda\s*organic|non[ -]?gmo|g\s*m\s*o|nongmoproject|wildcrafted|all\s*natural|100\s*%\s*natural|rainforest\s*alliance|fsc\s*(?:certified|certification))\b|(?:\bбио\b|\bг\s*м\s*о\b|биологич\w*\s+(?:продукт|земедел|сертиф|производ)|органич\w*\s+(?:продукт|сертиф|земедел|хран)|без\s+гмо|не\s*гмо)'
}
OTHER = {'unsupported_certification':r'\b(?:fda\s*(?:approved|certified)|clinically\s*proven|doctor\s*recommended|100\s*%\s*safe|no\s*side\s*effects|guaranteed\s*results)\b|(?:одобрен\w*\s+от\s+fda|клинично\s+доказан\w*|без\s+странични\s+ефекти|гарантиран\w*\s+резултат)'}
INGREDIENT_HOLD = re.compile(r'\b(?:cbd|thc|cannabidiol|cannabis|dmaa|dmha|dhea|huperzine|yohimbine)\b|канабидиол|канабис|йохимбин|хуперзин',re.I)
SAFETY_PREFIX = re.compile(r'^(?:забележка|предупреждени\w*|съхранение|годност|препоръчителна\s+дневна|начин\s+на\s+употреба|дозировка|не\s+се\s+|да\s+не\s+|консултирайте|warning|caution|storage|suggested\s+use|directions|do\s+not|consult\s+)',re.I)


def hits(text, rules):
    text=textnorm(text)
    return [{'rule':name,'match':m.group(0)} for name,pattern in rules.items() for m in list(re.finditer(pattern,text,re.I))[:6]]


def marketing_text(product):
    # Do not misclassify contraindications and mandatory safety directions as
    # promises of treatment. Product title and every marketing paragraph remain.
    blocks=[product.get('title','')]
    for line in product.get('claim_text_source',product.get('source_text','')).splitlines():
        if line.strip() and not SAFETY_PREFIX.search(line.strip()):blocks.append(line)
    return '\n'.join(blocks)


def text_screen(product):
    result=[]
    for h in hits(marketing_text(product),MEDICAL):result.append({'reason':'medical_claim_in_text',**h})
    for h in hits(marketing_text(product),OTHER):result.append({'reason':'unsupported_claim_in_text',**h})
    if INGREDIENT_HOLD.search(' '.join(product.get('ingredients',[]))+' '+product.get('title','')):
        result.append({'reason':'ingredient_requires_policy_review'})
    return result


def detect_origins(text, allowed):
    aliases={
        'United States':[r'u\.?\s*s\.?\s*a\.?',r'u\.?\s*s\.?',r'united\s+states(?:\s+of\s+america)?',r'съединените\s+щати',r'сащ'],
        'United Kingdom':[r'uk',r'united\s+kingdom',r'great\s+britain',r'великобритания'],
        'Bulgaria':[r'българия'], 'Germany':[r'германия',r'deutschland'],
        'Canada':[r'канада'], 'India':[r'индия'], 'Mainland China':[r'china',r'китай'],
        'Netherlands':[r'нидерландия',r'холандия'], 'Poland':[r'полша'],
        'France':[r'франция'], 'Italy':[r'италия'], 'Spain':[r'испания']}
    text=textnorm(text)
    prefix=r'(?:\b(?:made|manufactured|produced|grown)\s+in\s*[:\-]?\s*(?:the\s+)?|\bcountry\s+of\s+origin\s*[:\-]?\s*|произведен[оаи]?\s+в\s*|(?:държава|страна)\s+на\s+произход\s*[:\-]?\s*|произход\s*[:\-]\s*)'
    found=[]
    for country in allowed:
        patterns=[re.escape(country)]+aliases.get(country,[])
        for pattern in patterns:
            m=re.search(prefix+'('+pattern+r')(?!\w)',text,re.I)
            if m:found.append({'country':country,'evidence':m.group(0)});break
    return found


def neutral_description(product):
    # No therapeutic/eco paragraph is rewritten into a covert claim. Risky
    # products are held upstream. Optional eco marketing is simply not copied.
    lines=[product['title'],'Хранителна добавка.','Марка: '+product['brand']]
    if product.get('ingredients'):lines+=['Състав (според продуктовата страница/етикета):']+product['ingredients']
    for line in product.get('directions',[]):
        if line.lower().startswith('приложение:'):continue
        lines.append(line)
    return '\n\n'.join(dict.fromkeys(lines))[:30000]


class ImageReviewer:
    def __init__(self, cache='cache/images', languages='eng+bul', min_confidence=55, min_words=3, delay=.5, refresh=False):
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.languages=languages;self.min_confidence=min_confidence;self.min_words=min_words;self.delay=delay
        self.last=0
        self.refresh=refresh

    def preflight(self):
        if not shutil.which('tesseract'):raise RuntimeError('Tesseract OCR is required; no unscreened image export is allowed')
        p=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,timeout=20,check=True)
        missing=set(self.languages.split('+'))-set(p.stdout.splitlines()[1:])
        if missing:raise RuntimeError('Missing Tesseract languages: '+', '.join(sorted(missing)))

    def download(self,url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ['revita.bg','www.revita.bg']:raise ValueError('Image host outside revita.bg')
        key=hashlib.sha256(url.encode()).hexdigest();target=self.cache/(key+'.image')
        if target.exists() and not self.refresh:body=target.read_bytes()
        else:
            time.sleep(max(0,self.delay-(time.monotonic()-self.last)))
            self.last=time.monotonic()
            request=urllib.request.Request(url,headers={'User-Agent':'RevitaCatalogExporter/2.0','Accept':'image/*','Cache-Control':'no-cache'})
            with urllib.request.urlopen(request,timeout=40) as response:
                if urllib.parse.urlsplit(response.url).hostname not in ['revita.bg','www.revita.bg']:raise ValueError('Image redirected outside approved host')
                body=response.read(20*1024*1024+1)
            if len(body)>20*1024*1024:raise ValueError('Image larger than 20 MB')
            Image.open(io.BytesIO(body)).verify();target.write_bytes(body)
        image=Image.open(io.BytesIO(body))
        if image.width*image.height>20_000_000:raise ValueError('Image exceeds 20 megapixels')
        image.load()
        if min(image.size)<300:raise ValueError('Image too small to screen label text reliably')
        return body,image

    def inspect(self,url):
        result={'url':url,'status':'unknown','text':'','medical':[],'eco':[],'other':[],'origins':[]}
        try:
            body,img=self.download(url)
            key=hashlib.sha256(body).hexdigest();result['sha256']=key
            cache=self.cache/(key+'.ocr-'+self.languages+'.json')
            if cache.exists():data=json.loads(cache.read_text())
            else:
                gray=ImageOps.autocontrast(ImageOps.grayscale(img))
                scale=min(3.0,2560/max(gray.size))
                if scale>1:gray=gray.resize((int(gray.width*scale),int(gray.height*scale)),Image.Resampling.LANCZOS)
                pages=[]
                with tempfile.TemporaryDirectory() as temp:
                    path=Path(temp)/'label.png';gray.save(path)
                    for mode in [11,6]:
                        p=subprocess.run(['tesseract',str(path),'stdout','-l',self.languages,'--psm',str(mode),'tsv'],capture_output=True,text=True,timeout=60,check=True)
                        words=[];confidence=[]
                        for record in csv.DictReader(io.StringIO(p.stdout),delimiter='\t'):
                            word=(record.get('text') or '').strip()
                            try:conf=float(record.get('conf',-1))
                            except ValueError:continue
                            if word and conf>=0:words.append(word);confidence.append(conf)
                        pages.append({'text':' '.join(words),'words':len(words),'confidence':sum(confidence)/len(confidence) if confidence else 0})
                data={'passes':pages};cache.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            passes=data['passes'];result['text']='\n'.join(p['text'] for p in passes)
            result['medical']=hits(result['text'],MEDICAL);result['eco']=hits(result['text'],ECO);result['other']=hits(result['text'],OTHER)
            result['quality_ok']=any(p['words']>=self.min_words and p['confidence']>=self.min_confidence for p in passes)
            result['confidence']=max(p['confidence'] for p in passes)
            result['status']='medical' if result['medical'] else 'eco' if result['eco'] else 'other' if result['other'] else 'pass' if result['quality_ok'] else 'unknown'
        except Exception as exc:result['error']=str(exc)
        return result
