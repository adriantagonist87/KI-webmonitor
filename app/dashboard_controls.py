"""Set automatic monitoring explicitly; never interrupt a running or manual check."""
import store
import scheduling
import retry_jobs


def set_automation(pid, enabled):
    if type(enabled) is not int or enabled not in (0,1):
        raise ValueError('Automatik muss ein- oder ausgeschaltet sein.')
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        if not row:raise LookupError('Projekt nicht gefunden.')
        project=dict(row)
        if enabled and not project['enabled']:
            c.execute('UPDATE projects SET enabled=1,next_due=? WHERE id=?',
                      (scheduling.next_project_due(project),pid))
        elif not enabled:
            c.execute('UPDATE projects SET enabled=0 WHERE id=?',(pid,))
            retry_jobs.cancel_project(pid,connection=c)
            c.execute("""UPDATE runs SET status='cancelled',finished=?,error=?
                WHERE project_id=? AND origin='scheduled' AND status='queued'""",
                (store.now(),'Automatische Prüfung verworfen: Projekt pausiert.',pid))
    return {'id':pid,'enabled':enabled}
