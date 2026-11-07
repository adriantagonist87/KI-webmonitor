"""One immutable global report plus independently archived recipient projections."""
import json
import store,email_reports,distributions,report_mail

def records(parent_id,key,created,model,icons,test=False,only_distribution=None):
    bundle=email_reports.render(model,icons);email_reports.save(parent_id,bundle)
    parent={'id':parent_id,'period_key':key,'created':created,'start':model['start'],'end':model['end'],'content':bundle['text'],'audience':'admin','parent_id':None,'distribution_id':None,'distribution_alias':None,'recipient_email':None,'selection_snapshot':'{}','mail_status':'pending'}
    result=[parent]
    targets=[distributions.get(only_distribution)] if only_distribution else [d for d in distributions.all_lists() if d['enabled']]
    for d in targets:
        if not d or d['deleted_at']:continue
        filtered,selection=email_reports.for_distribution(model,d)
        child_bundle=email_reports.render(filtered,icons)
        for recipient in d['emails']:
            rid=store.uid();email_reports.save(rid,child_bundle)
            state='skipped' if not filtered['counts']['sources'] else 'preview' if test and not d['enabled'] else 'pending'
            result.append({'id':rid,'period_key':key+'-distribution-'+store.digest(d['id']+'\n'+recipient)[:32],
                'created':created,'start':model['start'],'end':model['end'],'content':child_bundle['text'],
                'audience':'distribution','parent_id':parent_id,'distribution_id':d['id'],'distribution_alias':d['alias'],
                'recipient_email':recipient,'selection_snapshot':json.dumps(selection),'mail_status':state})
    return result

def insert(connection,items):
    for item in items:
        keys=list(item)
        connection.execute('INSERT INTO reports('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[item[k] for k in keys])
        report_mail.event(connection,item['id'],item['mail_status'],'Gefilterter Empfängerbericht erstellt.' if item['audience']=='distribution' else 'Globaler Adminbericht erstellt.')

async def dispatch(ids,client_factory):
    import httpx
    async def sender(content,subject,test,html,images,recipient):
        try:
            async with client_factory(timeout=90) as client:
                response=await client.post('http://gateway:8000/smtp',json={'content':content,'subject':subject,'test':test,'html':html,'images':images,'recipient':recipient})
        except (httpx.ConnectError,httpx.ConnectTimeout):raise report_mail.TransportFailure(True) from None
        except httpx.HTTPError:raise report_mail.TransportFailure(False) from None
        try:data=response.json()
        except (ValueError,TypeError):raise report_mail.TransportFailure(False) from None
        if response.is_error:raise report_mail.TransportFailure(data.get('failure_kind')=='definite')
        if data.get('mode') not in ('preview','production','mailpit'):raise report_mail.TransportFailure(False)
        return data['mode']
    for rid in ids:
        await report_mail.deliver(rid,sender)
