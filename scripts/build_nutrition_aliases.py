#!/usr/bin/env python3
"""Build the hand-curated recipe-ingredient -> USDA nutrition alias table.

The `recipe1m-usda-links-canonical` table is a machine embedding-match (median
similarity ~0.77) and is too noisy to trust — it maps e.g. `chicken breast` ->
"Oscar Mayer, Chicken Breast", `all-purpose flour` -> "Potato flour". This
script writes a small *hand-verified* override for the highest-frequency raw
proteins / produce / staples, pinned to the canonical raw/plain USDA records,
which `nutrition_match.best_nutrition_match` consults first.

Each ALIAS entry is either an explicit `usda_id` (verified by inspecting
`usda-nutrients-v1`) or a `find`/`exclude` term set resolved against the USDA
food-name list (shortest match wins — "Onions, raw" over "Onions, sweet, raw").

    python3 scripts/build_nutrition_aliases.py            # writes the CSV
    python3 scripts/build_nutrition_aliases.py --check    # resolve + print, no write
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from recipe_wrangler.utils.env_loader import load_runtime_env  # noqa: E402

load_runtime_env()

from recipe_wrangler.utils.pipeline_data_pg import load_pipeline_data  # noqa: E402

OUTPUT = REPO_ROOT / "data/processed/fallbacks/ingredient_nutrition_aliases.csv"

# names: list of recipe-ingredient aliases (matched normalized + singularized)
# usda_id: explicit verified id  |  find/exclude: resolved against food_name list
ALIASES: list[dict] = [
    # --- poultry (raw, meat only / skinless boneless) ---
    {"names": ["chicken breast", "chicken breasts", "chicken breast fillet",
               "chicken breast fillets", "boneless skinless chicken breast",
               "skinless chicken breast", "boneless chicken breast", "chicken fillet"],
     "usda_id": "05062"},  # Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw
    {"names": ["chicken breast bone in", "bone in chicken breast", "chicken breast skin on"],
     "usda_id": "05057"},  # Chicken, broilers or fryers, breast, meat and skin, raw
    {"names": ["chicken thigh", "chicken thighs", "boneless skinless chicken thigh",
               "boneless skinless chicken thighs", "skinless chicken thigh", "boneless chicken thigh"],
     "usda_id": "05096"},  # Chicken, broilers or fryers, dark meat, thigh, meat only, raw
    {"names": ["chicken thigh bone in", "bone in chicken thigh", "chicken thighs skin on"],
     "usda_id": "05091"},  # Chicken, broilers or fryers, thigh, meat and skin, raw
    {"names": ["chicken drumstick", "chicken drumsticks", "chicken legs", "chicken leg"],
     "find": ["Chicken", "drumstick", "meat and skin", "raw"], "exclude": ["solution"]},
    {"names": ["ground chicken", "minced chicken", "chicken mince"],
     "find": ["Chicken", "ground", "raw"], "exclude": ["breaded", "patties"]},
    {"names": ["ground turkey", "minced turkey", "turkey mince"], "usda_id": "05305"},  # Turkey, Ground, raw
    {"names": ["turkey breast", "turkey breasts"],
     "find": ["Turkey", "breast", "meat only", "raw"], "exclude": ["solution", "smoked", "roll"]},
    # --- beef / pork / lamb (raw) ---
    {"names": ["ground beef", "minced beef", "beef mince", "lean ground beef", "ground chuck",
               "ground beef chuck", "hamburger", "hamburger meat", "lean mince"],
     "find": ["Beef", "ground", "85% lean meat", "raw"], "exclude": []},
    {"names": ["beef chuck", "chuck roast", "beef chuck roast", "stewing beef", "stew beef", "beef stew meat", "chuck steak"],
     "usda_id": "23093"},  # Beef, chuck for stew, separable lean and fat, all grades, raw
    {"names": ["beef sirloin", "sirloin steak", "beef sirloin steak"],
     "find": ["Beef", "loin", "top sirloin", "lean and fat", "raw"], "exclude": ["select", "choice"]},
    {"names": ["beef tenderloin", "filet mignon"],
     "find": ["Beef", "loin", "tenderloin", "lean and fat", "raw"], "exclude": ["select", "choice"]},
    {"names": ["pork loin", "pork loin chop", "pork chop", "pork chops", "boneless pork chop"],
     "usda_id": "10020"},  # Pork, fresh, loin, whole, separable lean and fat, raw
    {"names": ["pork tenderloin"],
     "find": ["Pork", "fresh", "tenderloin", "lean and fat", "raw"], "exclude": []},
    {"names": ["ground pork", "minced pork", "pork mince"],
     "find": ["Pork", "ground", "raw"], "exclude": ["breaded"]},
    {"names": ["ground lamb", "minced lamb", "lamb mince"],
     "find": ["Lamb", "ground", "raw"], "exclude": []},
    {"names": ["lamb chop", "lamb chops", "lamb loin chop"],
     "find": ["Lamb", "loin", "lean and fat", "raw"], "exclude": ["choice", "cooked", "ground", "trimmed"]},
    {"names": ["bacon", "streaky bacon", "back bacon", "rashers", "bacon rashers"],
     "find": ["Pork", "cured", "bacon", "unprepared"], "exclude": ["turkey", "canadian", "lower sodium", "reduced sodium"]},
    {"names": ["ham", "deli ham", "sliced ham"],
     "find": ["Ham", "sliced", "regular"], "exclude": ["minced", "extra lean", "turkey"]},
    # --- fish / seafood (raw) ---
    {"names": ["salmon", "salmon fillet", "salmon fillets", "salmon steak", "fresh salmon"],
     "find": ["Fish", "salmon", "Atlantic", "farmed", "raw"], "exclude": []},
    {"names": ["cod", "cod fillet", "cod fillets"], "usda_id": "15015"},  # Fish, cod, Atlantic, raw
    {"names": ["tilapia", "tilapia fillet", "tilapia fillets"],
     "find": ["Fish", "tilapia", "raw"], "exclude": []},
    {"names": ["tuna steak", "tuna fillet", "fresh tuna", "ahi tuna"],
     "find": ["Fish", "tuna", "fresh", "yellowfin", "raw"], "exclude": ["canned"]},
    {"names": ["shrimp", "prawns", "raw shrimp", "raw prawns", "jumbo shrimp", "tiger prawns"],
     "find": ["Crustaceans", "shrimp", "raw"], "exclude": ["imitation", "breaded", "cooked", "canned"]},
    {"names": ["tinned tuna", "canned tuna", "tuna in water", "tuna in spring water"],
     "find": ["Fish", "tuna", "light", "canned in water", "drained solids"], "exclude": ["white", "oil"]},
    # --- eggs / dairy ---
    {"names": ["egg", "eggs", "large egg", "large eggs", "whole egg", "whole eggs", "free range eggs", "free range egg"],
     "usda_id": "01123"},  # Egg, whole, raw, fresh
    {"names": ["egg white", "egg whites"], "find": ["Egg", "white", "raw", "fresh"], "exclude": ["dried"]},
    {"names": ["egg yolk", "egg yolks"], "find": ["Egg", "yolk", "raw", "fresh"], "exclude": ["dried"]},
    {"names": ["milk", "whole milk", "full fat milk", "full cream milk"], "usda_id": "01077"},  # Milk, whole, 3.25% milkfat, w/ added vit D
    {"names": ["skim milk", "skimmed milk", "nonfat milk", "non fat milk", "fat free milk"],
     "find": ["Milk", "nonfat", "skim", "fat free"], "exclude": ["dry", "evaporated", "buttermilk", "added"]},
    {"names": ["2% milk", "two percent milk", "reduced fat milk"], "usda_id": "01079"},  # Milk, reduced fat, fluid, 2% milkfat, w/ added vit A&D
    {"names": ["low fat milk", "1% milk", "lowfat milk"], "usda_id": "01082"},  # Milk, lowfat, fluid, 1% milkfat, w/ added vit A&D
    {"names": ["buttermilk", "low fat buttermilk", "cultured buttermilk"],
     "find": ["Milk", "buttermilk", "fluid", "cultured"], "exclude": ["dried", "low fat"]},
    {"names": ["heavy cream", "heavy whipping cream", "double cream", "thickened cream", "whipping cream"],
     "find": ["Cream", "fluid", "heavy whipping"], "exclude": []},
    {"names": ["light cream", "single cream", "pouring cream", "table cream"],
     "find": ["Cream", "fluid", "light"], "exclude": ["whipping", "sour", "half"]},
    {"names": ["sour cream", "soured cream"], "find": ["Cream", "sour", "cultured"], "exclude": ["reduced", "fat free", "imitation", "light"]},
    {"names": ["half and half"], "find": ["Cream", "fluid", "half and half"], "exclude": ["fat free", "lowfat"]},
    {"names": ["butter", "salted butter"], "usda_id": "01001"},  # Butter, salted
    {"names": ["unsalted butter", "sweet butter"], "usda_id": "01145"},  # Butter, without salt
    {"names": ["yogurt", "yoghurt", "plain yogurt", "plain yoghurt", "natural yogurt", "natural yoghurt"],
     "usda_id": "01116"},  # Yogurt, plain, whole milk
    {"names": ["greek yogurt", "greek yoghurt", "plain greek yogurt"],
     "find": ["Yogurt", "Greek", "plain", "whole milk"], "exclude": ["nonfat", "2%"]},
    {"names": ["low fat yogurt", "low fat yoghurt", "low-fat yogurt", "low-fat yoghurt", "low fat plain yogurt", "lowfat yogurt"], "usda_id": "01117"},  # Yogurt, plain, low fat
    {"names": ["nonfat yogurt", "non fat yogurt", "fat free yogurt", "fat-free yogurt", "0% yogurt", "skim milk yogurt", "nonfat plain yogurt", "fat free plain yogurt"], "usda_id": "01118"},  # Yogurt, plain, skim milk
    {"names": ["nonfat greek yogurt", "non fat greek yogurt", "0% greek yogurt", "fat free greek yogurt", "fat-free greek yogurt", "low fat greek yogurt"], "usda_id": "01256"},  # Yogurt, Greek, plain, nonfat
    {"names": ["part skim ricotta", "part-skim ricotta", "reduced fat ricotta", "low fat ricotta", "light ricotta"], "usda_id": "01037"},  # Cheese, ricotta, part skim milk
    {"names": ["light cream cheese", "low fat cream cheese", "low-fat cream cheese", "reduced fat cream cheese", "reduced-fat cream cheese", "whipped cream cheese"], "usda_id": "43274"},  # Cheese, cream, low fat
    {"names": ["neufchatel", "neufchatel cheese"], "usda_id": "01031"},  # Cheese, neufchatel
    {"names": ["light mayonnaise", "light mayo", "low fat mayonnaise", "low-fat mayonnaise", "reduced fat mayonnaise", "reduced-fat mayo"], "usda_id": "04641"},  # Salad dressing, mayonnaise, light
    {"names": ["reduced fat cheddar", "reduced-fat cheddar", "low fat cheddar", "low-fat cheddar", "reduced fat cheese", "low fat cheese"], "usda_id": "01260"},  # Cheese, cheddar, reduced fat
    {"names": ["low fat cottage cheese", "lowfat cottage cheese", "1% cottage cheese", "low-fat cottage cheese"], "usda_id": "01016"},  # Cheese, cottage, lowfat, 1% milkfat
    {"names": ["2% cottage cheese", "reduced fat cottage cheese"], "usda_id": "01015"},  # Cheese, cottage, lowfat, 2% milkfat
    {"names": ["light sour cream", "reduced fat sour cream", "reduced-fat sour cream", "low fat sour cream", "low-fat sour cream"], "usda_id": "01055"},  # Cream, sour, reduced fat, cultured
    {"names": ["fat free sour cream", "fat-free sour cream", "nonfat sour cream", "non fat sour cream"], "usda_id": "01180"},  # Sour cream, fat free
    {"names": ["dark chocolate", "bittersweet chocolate", "bitter chocolate", "plain chocolate", "70% dark chocolate"], "usda_id": "19903"},  # Chocolate, dark, 60-69% cacao solids
    {"names": ["semisweet chocolate", "semi-sweet chocolate", "semisweet chocolate chips", "semi-sweet chocolate chips", "chocolate chips", "dark chocolate chips", "plain chocolate chips", "cooking chocolate"], "usda_id": "19080"},  # Candies, semisweet chocolate
    {"names": ["milk chocolate", "milk chocolate chips"], "usda_id": "19120"},  # Candies, milk chocolate
    {"names": ["white chocolate", "white chocolate chips"], "usda_id": "19087"},  # Candies, white chocolate
    {"names": ["cocoa powder", "unsweetened cocoa powder", "cocoa", "dutch process cocoa", "cacao powder", "baking cocoa"], "usda_id": "19165"},  # Cocoa, dry powder, unsweetened
    {"names": ["sweetened condensed milk", "condensed milk"], "usda_id": "01095"},  # Milk, canned, condensed, sweetened
    {"names": ["evaporated milk", "tinned evaporated milk"], "usda_id": "01153"},  # Milk, canned, evaporated, with added vitamin A
    {"names": ["nonfat evaporated milk", "fat free evaporated milk", "skim evaporated milk"], "usda_id": "01097"},  # Milk, canned, evaporated, nonfat
    {"names": ["cheddar cheese", "cheddar", "sharp cheddar", "mature cheddar", "grated cheddar", "shredded cheddar", "tasty cheese"], "usda_id": "01009"},  # Cheese, cheddar
    {"names": ["mozzarella cheese", "mozzarella", "shredded mozzarella"],
     "find": ["Cheese", "mozzarella", "low moisture", "part-skim"], "exclude": ["whole milk", "fresh"]},
    {"names": ["parmesan cheese", "parmesan", "parmigiano", "grated parmesan"],
     "find": ["Cheese", "parmesan", "grated"], "exclude": ["hard", "shredded"]},
    {"names": ["feta cheese", "feta"], "usda_id": "01019"},  # Cheese, feta
    {"names": ["cream cheese"], "find": ["Cheese", "cream"], "exclude": ["fat free", "low fat", "whipped"]},
    {"names": ["ricotta cheese", "ricotta"], "find": ["Cheese", "ricotta", "whole milk"], "exclude": ["part skim"]},
    {"names": ["cottage cheese"], "find": ["Cheese", "cottage", "creamed", "large or small curd"], "exclude": ["lowfat", "nonfat", "reduced"]},
    # --- flours / grains / pasta / sugar ---
    {"names": ["all purpose flour", "all-purpose flour", "plain flour", "flour", "white flour", "wheat flour"],
     "find": ["Wheat flour", "white", "all-purpose", "enriched", "bleached"], "exclude": ["self-rising", "calcium", "unbleached"]},
    {"names": ["bread flour", "strong flour", "strong white flour"],
     "find": ["Wheat flour", "white", "bread", "enriched"], "exclude": ["calcium"]},
    {"names": ["whole wheat flour", "wholemeal flour", "wholewheat flour", "whole grain flour"],
     "find": ["Wheat flour", "whole-grain"], "exclude": ["soft", "pastry", "white"]},
    {"names": ["self raising flour", "self-raising flour", "self rising flour"],
     "find": ["Wheat flour", "white", "all-purpose", "self-rising", "enriched"], "exclude": []},
    {"names": ["cornstarch", "corn starch", "cornflour", "corn flour"], "find": ["Cornstarch"], "exclude": []},
    {"names": ["white rice", "long grain rice", "long-grain rice", "rice", "white long grain rice"],
     "usda_id": "20044"},  # Rice, white, long-grain, regular, raw, enriched
    {"names": ["brown rice", "long grain brown rice"],
     "find": ["Rice", "brown", "long-grain", "raw"], "exclude": ["medium", "precooked", "instant"]},
    {"names": ["basmati rice", "jasmine rice"],
     "find": ["Rice", "white", "long-grain", "parboiled", "unenriched"], "exclude": []},
    {"names": ["arborio rice", "risotto rice"],
     "find": ["Rice", "white", "short-grain", "raw"], "exclude": ["enriched", "cooked"]},
    {"names": ["pasta", "dried pasta", "spaghetti", "penne", "macaroni", "fusilli", "rigatoni", "linguine", "fettuccine"],
     "find": ["Pasta", "dry", "enriched"], "exclude": ["whole-wheat", "spinach", "corn", "gluten", "vegetable", "fresh", "cooked", "homemade"]},
    {"names": ["rolled oats", "porridge oats", "old fashioned oats", "oats", "oatmeal", "quick oats"], "usda_id": "08120"},  # Cereals, oats, regular and quick, not fortified, dry
    {"names": ["granulated sugar", "white sugar", "sugar", "caster sugar", "castor sugar", "superfine sugar"],
     "find": ["Sugars", "granulated"], "exclude": []},
    {"names": ["brown sugar", "light brown sugar", "dark brown sugar", "soft brown sugar"],
     "find": ["Sugars", "brown"], "exclude": []},
    {"names": ["powdered sugar", "icing sugar", "confectioners sugar", "confectioner's sugar", "10x sugar"],
     "find": ["Sugars", "powdered"], "exclude": []},
    {"names": ["honey", "raw honey", "clear honey"], "find": ["Honey"], "exclude": []},
    {"names": ["maple syrup", "pure maple syrup"], "find": ["Syrups", "maple"], "exclude": ["pancake", "table"]},
    # --- oils / fats ---
    {"names": ["olive oil", "extra virgin olive oil", "extra-virgin olive oil", "evoo"],
     "find": ["Oil", "olive", "salad or cooking"], "exclude": []},
    {"names": ["vegetable oil", "neutral oil", "cooking oil", "frying oil"], "usda_id": "04044"},  # Oil, soybean, salad or cooking
    {"names": ["canola oil", "rapeseed oil"], "find": ["Oil", "canola"], "exclude": []},
    {"names": ["sunflower oil"], "find": ["Oil", "sunflower", "linoleic"], "exclude": ["high oleic", "hydrogenated"]},
    {"names": ["sesame oil", "toasted sesame oil"], "find": ["Oil", "sesame", "salad or cooking"], "exclude": []},
    {"names": ["coconut oil"], "find": ["Oil", "coconut"], "exclude": []},
    # --- vegetables (raw) ---
    {"names": ["onion", "onions", "yellow onion", "brown onion", "white onion", "red onion", "spanish onion"],
     "usda_id": "11282"},  # Onions, raw
    {"names": ["spring onion", "spring onions", "scallion", "scallions", "green onion", "green onions"],
     "find": ["Onions", "spring or scallions", "tops and bulb", "raw"], "exclude": []},
    {"names": ["shallot", "shallots"], "find": ["Shallots", "raw"], "exclude": []},
    {"names": ["garlic", "garlic clove", "garlic cloves", "minced garlic", "crushed garlic", "fresh garlic"],
     "find": ["Garlic", "raw"], "exclude": ["powder"]},
    {"names": ["ginger", "fresh ginger", "ginger root", "root ginger"], "find": ["Ginger root", "raw"], "exclude": []},
    {"names": ["carrot", "carrots", "baby carrots"], "usda_id": "11124"},  # Carrots, raw
    {"names": ["celery", "celery stalk", "celery stalks", "celery sticks"], "usda_id": "11143"},  # Celery, raw
    {"names": ["potato", "potatoes", "russet potato", "russet potatoes", "white potato", "white potatoes", "baking potato"],
     "find": ["Potatoes", "russet", "flesh and skin", "raw"], "exclude": []},
    {"names": ["sweet potato", "sweet potatoes", "kumara", "yam"], "find": ["Sweet potato", "raw", "unprepared"], "exclude": ["canned", "frozen"]},
    {"names": ["tomato", "tomatoes", "fresh tomato", "fresh tomatoes", "ripe tomatoes", "vine tomatoes", "cherry tomatoes", "cherry tomato", "grape tomatoes"],
     "find": ["Tomatoes", "red", "ripe", "raw", "year round average"], "exclude": []},
    {"names": ["canned tomatoes", "tinned tomatoes", "chopped tomatoes", "diced tomatoes", "crushed tomatoes", "whole tomatoes"],
     "find": ["Tomatoes", "red", "ripe", "canned", "packed in tomato juice"], "exclude": ["stewed", "paste", "puree", "sauce", "with green chilies"]},
    {"names": ["tomato paste", "tomato puree"], "usda_id": "11546"},  # Tomato products, canned, paste, without salt added
    {"names": ["cucumber", "cucumbers", "english cucumber", "english cucumbers"], "find": ["Cucumber", "with peel", "raw"], "exclude": []},
    {"names": ["bell pepper", "red bell pepper", "green bell pepper", "yellow bell pepper", "capsicum", "red capsicum", "green capsicum", "sweet pepper", "bell peppers"],
     "find": ["Peppers", "sweet", "red", "raw"], "exclude": ["frozen", "canned", "freeze-dried"]},
    {"names": ["broccoli", "broccoli florets", "broccoli florettes"], "find": ["Broccoli", "raw"], "exclude": ["leaves", "stalks", "chinese", "raab", "frozen"]},
    {"names": ["cauliflower", "cauliflower florets"], "find": ["Cauliflower", "raw"], "exclude": ["green", "frozen"]},
    {"names": ["spinach", "baby spinach", "fresh spinach"], "usda_id": "11457"},  # Spinach, raw
    {"names": ["arugula", "rocket", "rocket leaves", "wild rocket"], "usda_id": "11959"},  # Arugula, raw
    {"names": ["kale", "curly kale", "baby kale"], "find": ["Kale", "raw"], "exclude": ["scotch", "frozen", "chinese"]},
    {"names": ["lettuce", "iceberg lettuce", "salad leaves", "mixed leaves", "mixed salad greens"],
     "find": ["Lettuce", "iceberg", "raw"], "exclude": []},
    {"names": ["romaine lettuce", "cos lettuce", "romaine"], "find": ["Lettuce", "cos or romaine", "raw"], "exclude": []},
    {"names": ["mushroom", "mushrooms", "button mushrooms", "white mushrooms", "white button mushrooms"],
     "usda_id": "11260"},  # Mushrooms, white, raw
    {"names": ["cremini mushrooms", "crimini mushrooms", "baby bella mushrooms", "chestnut mushrooms", "brown mushrooms"],
     "find": ["Mushrooms", "brown", "Italian, or Crimini", "raw"], "exclude": []},
    {"names": ["portobello mushrooms", "portabella mushrooms", "portobello mushroom"],
     "find": ["Mushrooms", "portabella", "raw"], "exclude": ["grilled", "exposed to ultraviolet"]},
    {"names": ["zucchini", "courgette", "courgettes", "zucchinis"],
     "find": ["Squash", "summer", "zucchini", "includes skin", "raw"], "exclude": []},
    {"names": ["eggplant", "aubergine", "aubergines", "eggplants"], "find": ["Eggplant", "raw"], "exclude": ["pickled"]},
    {"names": ["pumpkin", "pumpkin puree"], "find": ["Pumpkin", "raw"], "exclude": ["leaves", "flowers", "canned", "pie mix"]},
    {"names": ["butternut squash"], "find": ["Squash", "winter", "butternut", "raw"], "exclude": []},
    {"names": ["green beans", "green bean", "string beans", "french beans"], "find": ["Beans", "snap", "green", "raw"], "exclude": ["canned", "frozen", "yellow"]},
    {"names": ["peas", "green peas", "garden peas"], "find": ["Peas", "green", "raw"], "exclude": ["split", "edible-podded", "frozen", "canned", "mature"]},
    {"names": ["frozen peas"], "find": ["Peas", "green", "frozen", "unprepared"], "exclude": ["edible-podded"]},
    {"names": ["corn", "sweetcorn", "sweet corn", "corn kernels"], "find": ["Corn", "sweet", "yellow", "raw"], "exclude": ["white", "canned", "frozen", "creamed"]},
    {"names": ["asparagus"], "find": ["Asparagus", "raw"], "exclude": ["canned", "frozen"]},
    {"names": ["cabbage", "green cabbage", "white cabbage"], "find": ["Cabbage", "raw"], "exclude": ["red", "savoy", "chinese", "napa"]},
    # --- legumes (canned, drained — the common recipe form) ---
    {"names": ["canned chickpeas", "chickpeas", "garbanzo beans", "tinned chickpeas"],
     "find": ["Chickpeas", "garbanzo beans", "bengal gram", "mature seeds", "canned", "drained solids"], "exclude": ["solids and liquids", "low sodium"]},
    {"names": ["canned black beans", "black beans", "tinned black beans"], "usda_id": "16018"},  # Beans, black turtle, mature seeds, canned
    {"names": ["canned kidney beans", "kidney beans", "red kidney beans"],
     "find": ["Beans", "kidney", "red", "mature seeds", "canned", "drained solids"], "exclude": ["solids and liquids", "all types", "low sodium"]},
    {"names": ["canned cannellini beans", "cannellini beans", "white beans", "canned white beans", "great northern beans"],
     "find": ["Beans", "white", "mature seeds", "canned"], "exclude": ["low sodium"]},
    {"names": ["red lentils", "split red lentils"], "usda_id": "16144"},  # Lentils, pink or red, raw
    {"names": ["lentils", "brown lentils", "green lentils", "puy lentils"], "usda_id": "16069"},  # Lentils, raw
    # --- fruit (raw) ---
    {"names": ["apple", "apples", "granny smith apples", "gala apples"], "find": ["Apples", "raw", "with skin"], "exclude": ["canned", "dried", "frozen", "without skin"]},
    {"names": ["banana", "bananas", "ripe bananas"], "find": ["Bananas", "raw"], "exclude": ["dried", "red", "dehydrated"]},
    {"names": ["lemon", "lemons", "fresh lemon"], "find": ["Lemons", "raw", "without peel"], "exclude": []},
    {"names": ["lemon juice", "fresh lemon juice"], "find": ["Lemon juice", "raw"], "exclude": ["canned", "frozen", "bottled"]},
    {"names": ["lime", "limes", "fresh lime"], "find": ["Limes", "raw"], "exclude": []},
    {"names": ["lime juice", "fresh lime juice"], "find": ["Lime juice", "raw"], "exclude": ["canned", "bottled"]},
    {"names": ["orange", "oranges", "navel oranges"], "find": ["Oranges", "raw", "all commercial varieties"], "exclude": ["juice", "with peel"]},
    {"names": ["orange juice", "fresh orange juice"], "find": ["Orange juice", "raw"], "exclude": ["canned", "frozen", "chilled", "with calcium"]},
    {"names": ["avocado", "avocados", "ripe avocado", "hass avocado"], "find": ["Avocados", "raw", "all commercial varieties"], "exclude": ["california", "florida"]},
    {"names": ["strawberries", "strawberry", "fresh strawberries"], "find": ["Strawberries", "raw"], "exclude": ["frozen", "canned"]},
    {"names": ["blueberries", "blueberry"], "find": ["Blueberries", "raw"], "exclude": ["frozen", "canned", "dried", "wild"]},
    {"names": ["raspberries", "raspberry"], "find": ["Raspberries", "raw"], "exclude": ["frozen", "canned"]},
    {"names": ["mango", "mangoes", "mangos", "fresh mango"], "usda_id": "09176"},  # Mangos, raw
    {"names": ["pineapple", "fresh pineapple"], "find": ["Pineapple", "raw", "all varieties"], "exclude": ["canned", "frozen", "juice"]},
    # --- pantry / seasonings ---
    {"names": ["salt", "table salt", "fine salt", "cooking salt"], "find": ["Salt", "table"], "exclude": ["substitute", "low sodium"]},
    {"names": ["sea salt", "kosher salt", "flaky salt"], "find": ["Salt", "table"], "exclude": ["substitute", "low sodium"]},
    {"names": ["black pepper", "pepper", "ground black pepper", "freshly ground black pepper", "cracked black pepper"],
     "find": ["Spices", "pepper", "black"], "exclude": ["white", "red", "lemon"]},
    {"names": ["baking powder", "double acting baking powder"], "find": ["Leavening agents", "baking powder", "double-acting", "sodium aluminum sulfate"], "exclude": ["low-sodium", "straight phosphate"]},
    {"names": ["baking soda", "bicarbonate of soda", "bicarb soda", "sodium bicarbonate"], "find": ["Leavening agents", "baking soda"], "exclude": []},
    {"names": ["vanilla extract", "vanilla essence", "pure vanilla extract"], "usda_id": "02050"},  # Vanilla extract
    {"names": ["ground cinnamon", "cinnamon", "cinnamon powder"], "usda_id": "02010"},  # Spices, cinnamon, ground
    {"names": ["ground cumin", "cumin", "cumin powder"], "find": ["Spices", "cumin", "seed"], "exclude": []},
    {"names": ["paprika", "sweet paprika", "smoked paprika"], "find": ["Spices", "paprika"], "exclude": []},
    {"names": ["chili powder", "chilli powder"], "find": ["Spices", "chili powder"], "exclude": []},
    {"names": ["ground ginger", "ginger powder"], "find": ["Spices", "ginger", "ground"], "exclude": []},
    {"names": ["garlic powder"], "find": ["Spices", "garlic powder"], "exclude": []},
    {"names": ["onion powder"], "find": ["Spices", "onion powder"], "exclude": []},
    {"names": ["dried oregano", "oregano"], "find": ["Spices", "oregano", "dried"], "exclude": ["mexican"]},
    {"names": ["dried basil", "basil"], "find": ["Spices", "basil", "dried"], "exclude": []},
    {"names": ["dried thyme", "thyme"], "find": ["Spices", "thyme", "dried"], "exclude": []},
    {"names": ["fresh parsley", "parsley", "chopped parsley", "flat leaf parsley"], "find": ["Parsley", "fresh"], "exclude": ["dried", "freeze-dried"]},
    {"names": ["fresh cilantro", "cilantro", "fresh coriander", "coriander leaves", "chopped cilantro"], "find": ["Coriander", "cilantro", "leaves", "raw"], "exclude": ["dried"]},
    {"names": ["fresh basil", "basil leaves", "fresh basil leaves"], "find": ["Basil", "fresh"], "exclude": ["dried"]},
    {"names": ["fresh mint", "mint", "mint leaves", "fresh mint leaves"], "find": ["Spearmint", "fresh"], "exclude": ["dried"]},
    {"names": ["soy sauce", "light soy sauce", "dark soy sauce"], "find": ["Soy sauce made from soy and wheat", "shoyu"], "exclude": ["low sodium", "tamari", "less sodium"]},
    {"names": ["white wine vinegar", "wine vinegar"], "usda_id": "02068"},  # Vinegar, red wine (no white-wine vinegar in USDA SR)
    {"names": ["red wine vinegar"], "find": ["Vinegar", "red wine"], "exclude": []},
    {"names": ["balsamic vinegar"], "find": ["Vinegar", "balsamic"], "exclude": []},
    {"names": ["apple cider vinegar", "cider vinegar"], "find": ["Vinegar", "cider"], "exclude": []},
    {"names": ["white vinegar", "distilled vinegar"], "find": ["Vinegar", "distilled"], "exclude": []},
    {"names": ["mayonnaise", "mayo", "real mayonnaise", "full fat mayonnaise"], "usda_id": "04025"},  # Salad dressing, mayonnaise, regular
    {"names": ["mustard", "dijon mustard", "wholegrain mustard", "yellow mustard", "english mustard"], "find": ["Mustard", "prepared", "yellow"], "exclude": ["dijon", "low sodium"]},
    {"names": ["ketchup", "tomato ketchup", "tomato sauce"], "find": ["Catsup"], "exclude": ["low sodium", "reduced sodium"]},
    {"names": ["worcestershire sauce"], "find": ["Sauce", "worcestershire"], "exclude": []},
    {"names": ["chicken stock", "chicken broth", "chicken stock cube", "chicken bouillon"], "find": ["Soup", "stock", "chicken", "home-prepared"], "exclude": ["bouillon", "broth", "cube"]},
    {"names": ["beef stock", "beef broth", "beef bouillon"], "find": ["Soup", "beef", "broth or bouillon", "ready-to-serve"], "exclude": ["cube", "low sodium", "canned condensed"]},
    {"names": ["coconut milk", "canned coconut milk"], "usda_id": "12118"},  # Nuts, coconut milk, canned
    {"names": ["tofu", "firm tofu", "silken tofu", "extra firm tofu"], "find": ["Tofu", "raw", "firm", "prepared with calcium sulfate"], "exclude": ["fried", "salted", "fermented", "okara", "soft"]},
    {"names": ["peanut butter", "smooth peanut butter", "crunchy peanut butter"], "find": ["Peanut butter", "smooth style", "with salt"], "exclude": ["reduced fat", "low sodium", "chunk"]},
    {"names": ["bread", "white bread", "sliced bread"], "find": ["Bread", "white", "commercially prepared"], "exclude": ["toasted", "low sodium", "reduced calorie"]},
    {"names": ["breadcrumbs", "bread crumbs", "panko breadcrumbs", "panko"], "usda_id": "18079"},  # Bread, crumbs, dry, grated, plain
    {"names": ["almonds", "raw almonds", "whole almonds", "sliced almonds", "flaked almonds", "ground almonds"], "usda_id": "12061"},  # Nuts, almonds
    {"names": ["walnuts", "walnut halves", "chopped walnuts"], "find": ["Nuts", "walnuts", "english"], "exclude": ["black"]},
    {"names": ["cashews", "raw cashews", "cashew nuts"], "find": ["Nuts", "cashew nuts", "raw"], "exclude": ["dry roasted", "oil roasted"]},
    {"names": ["pecans", "pecan halves"], "find": ["Nuts", "pecans"], "exclude": ["dry roasted", "oil roasted"]},
    {"names": ["peanuts", "raw peanuts", "unsalted peanuts"], "find": ["Peanuts", "all types", "raw"], "exclude": ["dry-roasted", "oil-roasted", "boiled", "spanish", "valencia", "virginia"]},
    {"names": ["sesame seeds"], "find": ["Seeds", "sesame seeds", "whole", "dried"], "exclude": ["roasted", "toasted", "decorticated", "kernels"]},
    {"names": ["sunflower seeds"], "find": ["Seeds", "sunflower seed kernels", "dried"], "exclude": ["dry roasted", "oil roasted", "toasted"]},
    # --- alcohol (distilled / wine / beer) ---
    {"names": ["rum", "dark rum", "white rum", "light rum", "spiced rum", "golden rum"], "usda_id": "14050"},  # Alcoholic beverage, distilled, rum, 80 proof
    {"names": ["vodka"], "usda_id": "14051"},  # Alcoholic beverage, distilled, vodka, 80 proof
    {"names": ["gin", "whiskey", "whisky", "bourbon", "scotch", "brandy", "tequila", "cognac", "rye whiskey"], "usda_id": "14037"},  # Alcoholic beverage, distilled, all, 80 proof
    {"names": ["white wine", "dry white wine", "sauvignon blanc", "pinot grigio", "chardonnay"], "usda_id": "14106"},  # Alcoholic beverage, wine, table, white
    {"names": ["red wine", "dry red wine", "cabernet sauvignon", "merlot", "pinot noir", "chianti", "shiraz"], "usda_id": "14096"},  # Alcoholic beverage, wine, table, red
    {"names": ["beer", "lager", "ale", "pale ale", "pilsner"], "usda_id": "14003"},  # Alcoholic beverage, beer, regular, all
]


def _norm(s: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s or "").lower())).strip()


def _term_in(term: str, name_lc: str) -> bool:
    """Single-word terms match on word boundary (so 'milk' != 'milkfat'); multi-word
    terms and ones with %/digits match as a substring."""
    t = term.lower()
    if " " in t or "%" in t or any(ch.isdigit() for ch in t) or "-" in t:
        return t in name_lc
    return re.search(r"\b" + re.escape(t) + r"\b", name_lc) is not None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="resolve and print, don't write")
    args = parser.parse_args()

    rows = load_pipeline_data("usda-nutrients-v1")
    by_id = {str(r["usda_id"]): str(r["food_name"]) for r in rows}
    food_list = sorted(((str(r["usda_id"]), str(r["food_name"])) for r in rows), key=lambda t: len(t[1]))

    out_rows: list[dict] = []
    seen_aliases: set[str] = set()
    problems: list[str] = []

    for entry in ALIASES:
        names = entry["names"]
        if "usda_id" in entry:
            uid = str(entry["usda_id"])
            label = by_id.get(uid)
            if label is None:
                problems.append(f"explicit usda_id {uid} not found (aliases: {names[:2]})")
                continue
        else:
            terms = entry["find"]
            neg = entry.get("exclude", [])
            matches = [
                (i, n) for i, n in food_list
                if all(_term_in(t, n.lower()) for t in terms) and not any(_term_in(x, n.lower()) for x in neg)
            ]
            if not matches:
                problems.append(f"NO MATCH for find={entry['find']} exclude={entry.get('exclude')} (aliases: {names[:2]})")
                continue
            uid, label = matches[0]  # shortest food_name

        for alias in names:
            key = _norm(alias)
            if not key or key in seen_aliases:
                continue
            seen_aliases.add(key)
            out_rows.append({"alias": key, "usda_id": uid, "usda_food_name": label})

    out_rows.sort(key=lambda r: r["alias"])
    print(f"resolved {len(ALIASES)} entries -> {len(out_rows)} alias rows")
    if problems:
        print("\n!!! PROBLEMS:")
        for p in problems:
            print("  -", p)
    print("\n--- sample ---")
    for r in out_rows[:: max(1, len(out_rows) // 30)]:
        print(f"  {r['alias']:<34} -> {r['usda_id']}  {r['usda_food_name']}")

    if args.check:
        print("\n(--check: nothing written)")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["alias", "usda_id", "usda_food_name"])
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nwrote {OUTPUT}")


if __name__ == "__main__":
    main()
