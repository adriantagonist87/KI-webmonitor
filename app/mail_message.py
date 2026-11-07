"""One MIME builder; plain text + exact HTML + CID PNGs, no external image fetch."""
from email.message import EmailMessage
import report_images

def build(content,subject,sender,recipient,html=None,images=None):
    message=EmailMessage();message['Subject']=subject;message['From']=sender;message['To']=recipient
    message.set_content(content)
    if html:
        pictures=report_images.validate(images or [],html)
        message.add_alternative(html,subtype='html')
        part=message.get_payload()[-1]
        for cid,raw in pictures.items():
            part.add_related(raw,maintype='image',subtype='png',cid='<'+cid+'>',disposition='inline',filename=cid.split('@')[0]+'.png')
    elif images:raise ValueError('Bilder benötigen einen HTML-Bericht.')
    return message
