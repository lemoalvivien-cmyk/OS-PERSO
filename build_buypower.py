"""Build BuyPower SaaS - orchestrated via HERMES OS"""
import urllib.request, urllib.error, json, sys, time, os
if sys.platform == "win32": sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:9307"
CAHIER = """Voici ton mission : developper une application SaaS complete appelee "BuyPower".

STACK : Next.js 14+, TypeScript, Supabase (auth + DB), Tailwind CSS, shadcn/ui
THEME : Dark anthracite background, neon lime accent (#a3e635), style Cybertruck, glassmorphism
REPO : C:\\Users\\PC\\BuyPower

STRUCTURE DU PROJET :

Pages :
- / (Landing)
- /auth/login, /auth/register (choix role Buyer/Agency)
- /buyer/dashboard, /buyer/profile, /buyer/requests
- /agency/dashboard, /agency/buyers, /agency/requests
- /admin, /admin/users, /admin/subscriptions, /admin/content, /admin/settings
- /legal/cgu, /legal/privacy

Base de donnees (6 tables) :
1) profiles (id, role BUYER|AGENCY|ADMIN, full_name, phone, city, avatar_url, created_at)
2) buyer_intents (id, buyer_id, budget_min/max, city, zip_code, property_type, purchase_delay_months, must_have, nice_to_have, visibility bool, created_at, updated_at)
3) agency_profiles (id, agency_id, agency_name, siret unique, city, phone_number, website_url, validated_by_admin bool, created_at, updated_at)
4) subscriptions (id, agency_id, stripe_customer_id, stripe_subscription_id, status inactive|active|past_due|canceled, current_period_end, created_at, updated_at)
5) contact_requests (id, buyer_id, agency_id, message_from_agency, status PENDING|ACCEPTED|REFUSED, created_at, updated_at)
6) cms_blocks (id, page, section_key, title, subtitle, body, order_index)

Landing page : Hero H1 "L'immobilier inverse : ce sont les acheteurs qui fixent les regles." avec 2 CTA, sections acheteurs/agences, comment ca marche (3 etapes), FAQ, CTA final. Contenu charge depuis cms_blocks.

Auth : Supabase email/password, role-based routing (BUYER->/buyer/dashboard, AGENCY->/agency/dashboard, ADMIN email=contact@vlmconsulting.fr->/admin).

Acheteur : formulaire intention achat, dashboard resume, requests avec accepter/refuser.
Agence : profil agence, abonnement Stripe 490 euros/an, filtrer acheteurs anonymises, demander contact.
Admin : KPIs, gestion users, abonnements, CMS landing, settings Stripe.

ACTION IMMEDIATEE : Cree le projet Next.js dans C:\\Users\\PC\\BuyPower avec la commande : npx create-next-app@latest BuyPower --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --use-npm --no-turbopack

Puis installe les dependances : shadcn/ui, @supabase/supabase-js, next-themes, lucide-react, stripe

Puis cree TOUTES les pages et composants. C'est un ordre."""

def send(msg, timeout=120):
    data = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def send_cmd(cmd, timeout=60):
    data = json.dumps({"message": f"run {cmd}"}).encode()
    req = urllib.request.Request(BASE + "/api/chat", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

print("=== Envoi du cahier des charges a HERMES OS ===")
print("Message envoye, attente de reponse...")
r = send(CAHIER, timeout=120)
print(f"\nReponse HERMES :")
if r.get("text"): print(r["text"][:2000])
if r.get("stdout"): print(f"\nStdout: {r['stdout'][:2000]}")
if r.get("image"): print(f"\n[Image capture: {len(r['image'])} chars base64]")
print(f"\nFull response keys: {list(r.keys())}")
