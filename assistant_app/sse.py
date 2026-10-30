"""Incremental SSE framing over HTTPX's standard UTF-8/line decoder."""
import httpx


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, source, observe):
        self.source, self.observe = source, observe

    async def __aiter__(self):
        async for chunk in self.source:
            self.observe(chunk)
            yield chunk

    async def aclose(self):
        await self.source.aclose()


async def events(response):
    data, kind = [], 'message'
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield kind, '\n'.join(data)
            data, kind = [], 'message'
        elif not line.startswith(':'):
            field, _, value = line.partition(':')
            if value.startswith(' '):
                value = value[1:]
            if field == 'data':
                data.append(value)
            elif field == 'event':
                kind = value
