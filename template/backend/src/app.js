const express = require('express');
const cors = require('cors');
const bodyParser = require('body-parser');
const fs = require('fs');
const path = require('path');
const app = express();
const {readPageData, renderPageHtml} = require('./page-bootstrap');
const {loadPageData} = require('./page-data');

// route modules imports

// middleware imports
app.use(cors());
app.use(bodyParser.json());
app.use(express.urlencoded({extended:false}));

// initialize database
const { initializeDatabase } = require('./database/init_db');
const databaseReady = initializeDatabase();
databaseReady.catch((error) => {
  console.error('Database initialization failed:', error);
});

// register routes
app.get('/api/health', (req, res) => {
  res.json({ code: 200, message: 'Backend Ready' });
});

const frontendDistPath = path.resolve(__dirname, '../../frontend/dist');

// Register after the application's session middleware. Also used by Vite's HTML
// transform, forwarding the same browser credentials to this local backend.
app.get('/api/page-data', async (req, res) => {
  res.set('Cache-Control', 'no-store');
  try {
    await databaseReady;
    res.json(await readPageData(req, req.query.path, loadPageData));
  } catch (error) {
    console.error('Page data failed:', error);
    res.status(503).json({error:'Page data unavailable'});
  }
});

if (fs.existsSync(frontendDistPath)) {
  const indexHtml = fs.readFileSync(path.join(frontendDistPath, 'index.html'), 'utf8');
  const staticAssets = express.static(frontendDistPath, {index:false});
  app.use((req, res, next) => /\.html?$/i.test(req.path) ? next() : staticAssets(req, res, next));

  // Keep API routes on the backend and serve the SPA for all other GET requests.
  app.get(/^(?!\/api(?:\/|$)).*/, async (req, res) => {
    res.set('Cache-Control', 'no-store');
    try {
      await databaseReady;
      const data = await readPageData(req, req.originalUrl, loadPageData);
      res.type('html').send(renderPageHtml(indexHtml, data));
    } catch (error) {
      console.error('Page data failed:', error);
      res.status(503).type('text').send('Page data unavailable');
    }
  });
} else {
  app.get('/', (req, res) => {
    res
      .status(503)
      .type('html')
      .send(`<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Frontend Build Missing</title>
    <style>
      body {
        margin: 0;
        font-family: ui-sans-serif, system-ui, sans-serif;
        background: #f6f7f9;
        color: #1f2937;
      }
      main {
        max-width: 720px;
        margin: 12vh auto 0;
        padding: 24px;
      }
      section {
        background: #fff;
        border: 1px solid #d1d5db;
        border-radius: 12px;
        padding: 24px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
      }
      h1 {
        margin-top: 0;
      }
      code {
        background: #f3f4f6;
        padding: 2px 6px;
        border-radius: 6px;
      }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>Frontend build missing</h1>
        <p>The backend is running, but <code>frontend/dist</code> is not available yet.</p>
        <p>Build the frontend first, then start the backend so it can host the compiled site on the same port.</p>
      </section>
    </main>
  </body>
</html>`);
  });
}

module.exports = app;
