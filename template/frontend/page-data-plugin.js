import bootstrap from '../backend/src/page-bootstrap.js';
import {AsyncLocalStorage} from 'node:async_hooks';

// Development and production use the same serializer/loader contract. No fetch
// during vite build; the production backend injects request-scoped data instead.
export function pageDataPlugin(backendPort) {
  const requests=new AsyncLocalStorage();
  return {
    name:'page-data',
    config() {return {server:{headers:{'Cache-Control':'no-store'}}};},
    configureServer(server) {
      server.middlewares.use((request, response, next)=>requests.run(request.headers,next));
    },
    transformIndexHtml: {
      order:'pre',
      async handler(html, context) {
        if (!context.server) return html;
        const headers={};
        for (const name of ['cookie','authorization']) {
          const value=requests.getStore()?.[name];
          if(typeof value==='string')headers[name]=value;
        }
        const request=context.originalUrl || context.path;
        const response=await fetch(`http://127.0.0.1:${backendPort}/api/page-data?path=${encodeURIComponent(request)}`,
          {headers,redirect:'error'});
        if(!response.ok)throw new Error(`Page data unavailable (${response.status})`);
        return bootstrap.renderPageHtml(html,await response.json());
      },
    },
  };
}
