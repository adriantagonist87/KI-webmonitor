"""Canonical project detail route used by the UI and absolute mail links."""
from urllib.parse import quote
PROJECT_DETAIL_ROUTE='/projects/{pid}'
def detail_path(pid):return PROJECT_DETAIL_ROUTE.format(pid=quote(str(pid),safe=''))
