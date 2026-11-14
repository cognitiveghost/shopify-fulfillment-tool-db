# Supabase (local)

CLI v3.1.0, no sudo:
`curl -sL https://github.com/supabase/cli/releases/download/v3.1.0/supabase_3.1.0_linux_amd64.tar.gz | tar xz -C ~/.local/bin supabase`

Local stack on default ports (API 54321, DB 54322, Studio 54323). Never `link`/`db push` from here.

    supabase start
    supabase db reset && supabase test db
