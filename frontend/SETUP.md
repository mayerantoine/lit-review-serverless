# Literature Review Frontend Setup Guide

## Summary of Changes

The Next.js frontend has been successfully configured to work with the FastAPI backend. All necessary dependencies, configurations, and API integrations have been completed.

## What Was Done

### 1. Dependencies Installed (`package.json`)
Added the following npm packages:
- `@microsoft/fetch-event-source` - For Server-Sent Events (SSE) streaming
- `react-markdown` - For rendering generated markdown text
- `remark-gfm` - GitHub Flavored Markdown support
- `remark-breaks` - Line break support in markdown

### 2. Next.js Configuration (`next.config.ts`)
- Added API proxy to forward `/api/*` requests to FastAPI backend at `http://localhost:8000`
- This allows the frontend to make requests to `/api/upload-and-index` without CORS issues

### 3. Layout Updates (`app/layout.tsx`)
- Updated metadata (title, description)
- Added Material Symbols font link for icons
- Configured SEO-friendly titles

### 4. Global Styles (`app/globals.css`)
- Added Tailwind `primary` color variable (`#1173d4`)
- Added Material Symbols icon styles
- Added custom scrollbar styles for better UX
- Configured dark mode support

### 5. Main Page (`app/page.tsx`)
- Migrated complete UI from `index.tsx`
- Fixed all API response parsing to match backend JSON structure:
  - `result.data.top_k_papers` instead of `result.top_papers`
  - `result.data.all_scored_papers` instead of `result.all_scored_papers`
  - `result.data.retrieval_stats` instead of `result.retrieval_stats`
  - `result.data.total_abstracts` instead of `result.total_abstracts`
- Implements three-phase workflow:
  1. Upload & Index CSV files
  2. Retrieve & Rank papers with LLM scoring
  3. Generate literature review with streaming

### 6. TypeScript Declarations (`types.d.ts`)
- Created type definitions for `@microsoft/fetch-event-source`
- Ensures proper TypeScript support for SSE streaming

### 7. Cleanup
- Removed old `app/index.tsx` file (migrated to `page.tsx`)

## Installation & Running

### Install Dependencies

```bash
cd frontend
npm install
```

This will install all dependencies from the updated `package.json`.

### Start Development Server

```bash
npm run dev
```

The frontend will be available at `http://localhost:3000`

### Start Production Build

```bash
npm run build
npm start
```

## Usage

1. **Start the FastAPI backend first:**
   ```bash
   cd backend
   uvicorn server:app --reload --port 8000
   ```

2. **Start the Next.js frontend:**
   ```bash
   cd frontend
   npm run dev
   ```

3. **Open your browser:**
   Navigate to `http://localhost:3000`

## API Endpoints Used

The frontend communicates with these FastAPI endpoints:

- **POST /api/upload-and-index** - Upload CSV and create vector index
- **POST /api/retrieve-and-rank** - Retrieve and score papers
- **POST /api/generate** - Generate literature review (SSE streaming)

## Features

### File Upload
- Drag & drop CSV upload
- File validation (max 50MB, CSV format)
- Progress indicators
- Real-time indexing status

### Paper Retrieval & Ranking
- Configurable hybrid-k parameter (1-200 papers)
- LLM-based relevance scoring
- Displays retrieval and scoring statistics
- Visual ranking display with scores

### Paper Selection
- **Top K Mode**: Select top N papers by relevance score
- **Min Score Mode**: Filter papers above minimum score threshold
- Real-time selection preview
- Visual indicators for selected papers

### Literature Review Generation
- Server-Sent Events (SSE) streaming for real-time text generation
- Live text rendering as it's generated
- Automatic citation extraction and display
- Markdown export functionality
- Professional formatting with references section

### UI/UX
- Split-panel responsive layout
- Dark mode support
- Loading states with progress messages
- Error handling with user-friendly messages
- Material Symbols icons
- Tailwind CSS styling

## File Structure

```
frontend/
├── app/
│   ├── favicon.ico          # App icon
│   ├── globals.css           # Global styles with Tailwind
│   ├── layout.tsx            # Root layout with metadata
│   └── page.tsx              # Main home page component
├── public/                   # Static assets
├── next.config.ts            # Next.js config with API proxy
├── package.json              # Dependencies
├── tsconfig.json             # TypeScript config
├── types.d.ts                # Custom type declarations
└── SETUP.md                  # This file
```

## Environment Variables

The frontend uses the Next.js API proxy, so no environment variables are needed for API URLs. The proxy forwards requests to `http://localhost:8000`.

If you need to change the backend URL, update `next.config.ts`:

```typescript
async rewrites() {
  return [
    {
      source: '/api/:path*',
      destination: 'http://your-backend-url:8000/api/:path*',
    },
  ];
},
```

## Troubleshooting

### Port Already in Use
If port 3000 is already in use:
```bash
npm run dev -- -p 3001
```

### API Connection Issues
1. Ensure FastAPI backend is running on port 8000
2. Check browser console for CORS errors
3. Verify Next.js proxy configuration in `next.config.ts`

### TypeScript Errors
If you see TypeScript errors:
```bash
npm run build
```
This will show all type errors that need fixing.

### Missing Icons
If Material Symbols icons don't load:
1. Check internet connection (font loads from Google Fonts)
2. Verify the font link in `app/layout.tsx`

## Next Steps

### For Development:
1. Test all three workflow phases
2. Try different CSV files (samples in `backend/uploads/`)
3. Experiment with different hybrid-k and top-k values
4. Test dark mode toggle

### For Production:
1. Set up environment-specific backend URLs
2. Configure proper error tracking (Sentry, etc.)
3. Add analytics (Google Analytics, Plausible, etc.)
4. Set up CI/CD pipeline
5. Deploy backend and frontend separately
6. Configure CORS properly for production domains

## Architecture Notes

- **Session Management**: Cookie-based sessions (24-hour expiry)
- **State Management**: React hooks (useState, useEffect)
- **API Communication**: Fetch API + Server-Sent Events
- **Styling**: Tailwind CSS v4
- **Type Safety**: TypeScript with strict mode
- **Rendering**: Client-side (marked with "use client")

## Support

For issues or questions:
1. Check browser console for errors
2. Check FastAPI logs for backend errors
3. Review `API_USAGE.md` in backend directory
4. Verify CSV file format matches requirements

---

**Congratulations! Your literature review application is now fully set up and ready to use!** 🎉
