<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <style>
        :root {
            --bg: #0f171e;
            --fg: #fff;
            --accent: #00a8e1;
            --radius: 6px;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--bg);
            color: var(--fg);
            font-family: 'Helvetica Neue', Arial, sans-serif;
            padding: 1rem;
            text-align: center;
        }
        h1 {
            font-size: 1.5rem;
            margin-bottom: 1rem;
        }
        img {
            max-width: 100%;
            height: auto;
            border-radius: var(--radius);
            margin-bottom: 1rem;
        }
        .btn {
            display: inline-block;
            padding: 0.75rem 1.5rem;
            background: var(--accent);
            color: var(--fg);
            text-decoration: none;
            border-radius: var(--radius);
            font-size: 1rem;
        }
    </style>
</head>
<body>
    <h1>{{ title }}</h1>
    <img src="{{ thumb_url }}" alt="{{ title }} thumbnail" onerror="this.src='/static/fallback.jpg';">
    <a href="https://t.me/{{ bot_username }}?start={{ key }}" class="btn">Get File</a>
</body>
</html>
