"""Gateway-only transports. Never expose tokens, upstream bodies or exception URLs."""
import smtplib
import ssl
from email.message import EmailMessage
import httpx
import communication_config as config
import telegram_message
from store import settings, credentials


class DeliveryError(Exception):
    def __init__(self, message, *, retry_after=None, uncertain=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.uncertain = uncertain


def smtp_client(s):
    errors={**s.get('_communication_errors',{}),**credentials().get('_communication_errors',{})}
    if any(k.startswith('smtp_') for k in errors):raise DeliveryError('SMTP-Konfiguration ungültig; Portainer-Vorgaben prüfen.')
    host = config.validate('smtp_host', s.get('smtp_host',''))
    if not host: raise DeliveryError('SMTP-Server fehlt.')
    port = config.validate('smtp_port', s.get('smtp_port',587))
    mode = config.validate('smtp_tls', s.get('smtp_tls','starttls'))
    cls = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    kwargs = {'context': ssl.create_default_context()} if mode == 'ssl' else {}
    client = cls(host, port, timeout=20, **kwargs)
    try:
        client.ehlo()
        if mode == 'starttls':
            client.starttls(context=ssl.create_default_context()); client.ehlo()
        if s.get('smtp_user'): client.login(s['smtp_user'], credentials().get('smtp_password',''))
        return client
    except Exception:
        client.close(); raise


def smtp_probe(send=False):
    s = settings()
    # Explicit test uses the configured account even when automatic mail is off.
    with smtp_client(s) as client:
        if not send:
            code, _ = client.noop()
            if code != 250: raise DeliveryError('SMTP-Server bestätigt die Verbindung nicht.')
        else:
            sender = config.validate('smtp_from',s.get('smtp_from',''))
            recipient = config.validate('smtp_to',s.get('smtp_to',''))
            if not sender or not recipient: raise DeliveryError('Absender und Empfänger fehlen.')
            msg=EmailMessage();msg['Subject']='KI-Webmonitor – Testmail'
            msg['From']=sender;msg['To']=recipient
            msg.set_content('Die Testmail des KI-Webmonitors wurde erfolgreich versandt.')
            msg.add_alternative('<html><body style="font:15px Arial;color:#252a32"><h2 style="color:#1967d2">KI-Webmonitor</h2><p>Die SMTP-Testmail wurde erfolgreich versandt.</p></body></html>',subtype='html')
            client.send_message(msg)
    return {'ok':True,'message':'Testmail vom SMTP-Server angenommen.' if send else 'SMTP-Verbindung, TLS und gegebenenfalls Anmeldung erfolgreich.'}


def telegram_call(method, payload=None):
    if method not in ('getMe','getChat','sendMessage'): raise DeliveryError('Telegram-Methode nicht erlaubt.')
    secret=credentials()
    if 'telegram_token' in secret.get('_communication_errors',{}):raise DeliveryError('Bot-Token hat ein ungültiges Format; Portainer-Vorgabe prüfen.')
    token=config.validate('telegram_token',secret.get('telegram_token',''))
    if not token: raise DeliveryError('Telegram-Bot-Token fehlt.')
    try:
        with httpx.Client(timeout=httpx.Timeout(20,connect=8),follow_redirects=False,trust_env=False) as client:
            response=client.post('https://api.telegram.org/bot'+token+'/'+method,json=payload or {})
        try: result=response.json()
        except ValueError: raise DeliveryError('Telegram-Antwort nicht lesbar.',uncertain=method=='sendMessage') from None
        if response.status_code==429:
            delay=(result.get('parameters') or {}).get('retry_after',60)
            delay=delay if type(delay) is int and delay>0 else 60
            raise DeliveryError('Telegram begrenzt die Anfragen.',retry_after=min(delay,86400)+1)
        if response.is_error or result.get('ok') is not True:
            code=response.status_code if response.is_error else result.get('error_code',400)
            messages={400:'Telegram: Chat-ID oder Nachricht ungültig.',401:'Telegram: Bot-Token abgewiesen.',403:'Telegram: Bot gesperrt oder kein Zugriff auf den Chat.',404:'Telegram: Bot nicht gefunden.'}
            raise DeliveryError(messages.get(code,'Telegram-Dienst nicht verfügbar.'),retry_after=60 if type(code) is int and code>=500 else None)
        return result.get('result',{})
    except (httpx.ConnectError,httpx.ConnectTimeout):
        raise DeliveryError('Telegram-Verbindung nicht hergestellt.',retry_after=60) from None
    except httpx.HTTPError:
        raise DeliveryError('Telegram-Übertragung unterbrochen; Zustellung unklar.',uncertain=method=='sendMessage') from None


def telegram_probe(send=False):
    identity=telegram_call('getMe')
    chat=config.validate('telegram_chat_id',settings().get('telegram_chat_id',''))
    if not chat: raise DeliveryError('Telegram-Chat-ID fehlt.')
    telegram_call('getChat',{'chat_id':chat})
    if send:
        telegram_call('sendMessage',{'chat_id':chat,'text':'KI-Webmonitor: Die Telegram-Testnachricht ist angekommen.','link_preview_options':{'is_disabled':True}})
    return {'ok':True,'message':'Telegram-Testnachricht versandt.' if send else 'Bot und Zielchat erreichbar.'}


def telegram_send(text):
    chat=config.validate('telegram_chat_id',settings().get('telegram_chat_id',''))
    if not chat: raise DeliveryError('Telegram-Chat-ID fehlt.')
    if not isinstance(text,str) or not text:
        raise DeliveryError('Telegram-Nachricht überschreitet die zulässige Länge.')
    text, entities = telegram_message.with_source_link(text)
    if len(text.encode('utf-16-le'))//2>4096:
        raise DeliveryError('Telegram-Nachricht überschreitet die zulässige Länge.')
    payload = {'chat_id':chat,'text':text,'link_preview_options':{'is_disabled':True}}
    if entities:
        payload['entities'] = entities
    telegram_call('sendMessage',payload)
    return {'ok':True}


def safe_failure(exc, channel):
    if isinstance(exc,(DeliveryError,config.base.ConfigurationError)): message=str(exc)
    elif isinstance(exc,smtplib.SMTPAuthenticationError): message='SMTP-Anmeldung abgewiesen.'
    elif isinstance(exc,ssl.SSLError): message='TLS-Verbindung oder Zertifikatsprüfung fehlgeschlagen.'
    elif isinstance(exc,smtplib.SMTPRecipientsRefused): message='SMTP-Empfänger abgewiesen.'
    else: message=channel+'-Verbindung fehlgeschlagen ('+type(exc).__name__+').'
    return {'ok':False,'error':message,'retry_after':getattr(exc,'retry_after',None),'uncertain':getattr(exc,'uncertain',False)}
