import datetime
import os
import pytz
from dotenv import load_dotenv
from notion_client import Client
import myfitnesspal

# Load environment variables from .env file
load_dotenv()

# --- Environment Variables ---
# Required:
# MYFITNESSPAL_USERNAME = os.getenv("MYFITNESSPAL_USERNAME") # No password needed if using cookies
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_MFP_DATABASE_ID") # Ensure this is different from Garmin DB ID

# Your local time zone, replace with the appropriate one if needed
# This is important for fetching the correct day's data from MyFitnessPal
LOCAL_TIMEZONE_STR = os.getenv("LOCAL_TIMEZONE", 'Etc/GMT') # Default to GMT if not set
local_tz = pytz.timezone(LOCAL_TIMEZONE_STR)

# --- Notion Database Property Names (MUST MATCH YOUR NOTION SETUP) ---
# These are examples. You'll need to create these properties in your Notion database.
NOTION_DATE_PROP = "Date"
NOTION_CALS_IN_PROP = "Calories In"
NOTION_CALS_OUT_PROP = "Calories Out (Exercise)" # Calories burned via logged exercise
NOTION_NET_CALS_PROP = "Net Calories"
NOTION_PROTEIN_PROP = "Protein (g)"
NOTION_CARBS_PROP = "Carbs (g)"
NOTION_FATS_PROP = "Fats (g)"
NOTION_WATER_PROP = "Water (ml)" # Optional

def get_mfp_client():
    """
    Initializes and returns the MyFitnessPal client.
    Relies on browser_cookie3 to find login cookies.
    The user must be logged into MyFitnessPal in their default browser.
    """
    # username = os.getenv("MYFITNESSPAL_USERNAME") # Not strictly needed for cookie auth
    # client = myfitnesspal.Client(username)
    # If the above doesn't work due to cookie issues or you want specific browser:
    # import browser_cookie3
    # cj = browser_cookie3.chrome() # Or .firefox(), .edge(), etc.
    # client = myfitnesspal.Client(cookiejar=cj)

    # Default client, attempts to find cookies from common browsers
    client = myfitnesspal.Client()
    return client

def get_mfp_data_for_date(client, target_date):
    """
    Fetches MyFitnessPal nutritional data for a given date.
    Args:
        client: The MyFitnessPal client object.
        target_date: A datetime.date object.
    Returns:
        A dictionary containing the nutritional data, or None if an error occurs.
    """
    try:
        day = client.get_date(target_date.year, target_date.month, target_date.day)

        if not day:
            print(f"No data found for {target_date.strftime('%Y-%m-%d')}")
            return None

        calories_in = day.totals.get('calories', 0)
        protein = day.totals.get('protein', 0)
        carbs = day.totals.get('carbohydrates', 0)
        fats = day.totals.get('fat', 0)
        sugar = day.totals.get('sugar', 0) # Example, not in main props for now
        sodium = day.totals.get('sodium', 0) # Example

        water_ml = client.get_water(target_date) # Fetches water intake in ml

        # Calculate Calories Out from exercises
        calories_out_exercise = 0
        if day.exercises:
            for exercise_group in day.exercises: # day.exercises is a list of Exercise objects
                for entry in exercise_group.entries: # Each Exercise has entries
                     calories_out_exercise += entry.nutrition_information.get('calories burned', 0)


        net_calories = calories_in - calories_out_exercise # This is a simple net

        # MyFitnessPal's own calculation for net calories might be different
        # if it considers BMR adjustments based on activity.
        # For goals and remaining:
        # mfp_goal_calories = day.goals.get('calories', 0)
        # mfp_remaining_calories = mfp_goal_calories - calories_in + calories_out_exercise

        return {
            "date": target_date.strftime('%Y-%m-%d'),
            "calories_in": round(calories_in or 0),
            "calories_out_exercise": round(calories_out_exercise or 0),
            "net_calories": round(net_calories or 0),
            "protein": round(protein or 0),
            "carbs": round(carbs or 0),
            "fats": round(fats or 0),
            "water_ml": water_ml if water_ml is not None else 0
        }
    except Exception as e:
        print(f"Error fetching MyFitnessPal data for {target_date.strftime('%Y-%m-%d')}: {e}")
        return None

def entry_exists(notion_client, database_id, entry_date_str):
    """
    Checks if an entry for the given date already exists in the Notion database.
    Args:
        notion_client: The Notion client.
        database_id: The ID of the Notion database.
        entry_date_str: The date string in 'YYYY-MM-DD' format.
    Returns:
        The Notion page object if it exists, otherwise None.
    """
    try:
        response = notion_client.databases.query(
            database_id=database_id,
            filter={
                "property": NOTION_DATE_PROP,
                "date": {
                    "equals": entry_date_str,
                }
            }
        )
        if response and response['results']:
            return response['results'][0]
        return None
    except Exception as e:
        print(f"Error checking if entry exists for {entry_date_str}: {e}")
        return None

def entry_needs_update(existing_page, new_data):
    """
    Compares an existing Notion page with new MyFitnessPal data to see if an update is needed.
    Args:
        existing_page: The existing Notion page object.
        new_data: A dictionary of the new MyFitnessPal data.
    Returns:
        True if an update is needed, False otherwise.
    """
    props = existing_page['properties']

    # Helper to safely get number from Notion property
    def get_notion_number(prop_name):
        if prop_name in props and props[prop_name]['number'] is not None:
            return props[prop_name]['number']
        return 0 # Default to 0 if property missing or None

    if (get_notion_number(NOTION_CALS_IN_PROP) != new_data["calories_in"] or
            get_notion_number(NOTION_CALS_OUT_PROP) != new_data["calories_out_exercise"] or
            get_notion_number(NOTION_NET_CALS_PROP) != new_data["net_calories"] or
            get_notion_number(NOTION_PROTEIN_PROP) != new_data["protein"] or
            get_notion_number(NOTION_CARBS_PROP) != new_data["carbs"] or
            get_notion_number(NOTION_FATS_PROP) != new_data["fats"] or
            (NOTION_WATER_PROP in props and get_notion_number(NOTION_WATER_PROP) != new_data["water_ml"])):
        return True
    # If water prop doesn't exist yet, and new data has water, it needs update
    if NOTION_WATER_PROP not in props and new_data.get("water_ml", 0) > 0:
        return True
    return False

def create_notion_entry(notion_client, database_id, mfp_data):
    """
    Creates a new entry in the Notion database.
    Args:
        notion_client: The Notion client.
        database_id: The ID of the Notion database.
        mfp_data: A dictionary of MyFitnessPal data for the day.
    """
    properties = {
        NOTION_DATE_PROP: {"date": {"start": mfp_data["date"]}},
        NOTION_CALS_IN_PROP: {"number": mfp_data["calories_in"]},
        NOTION_CALS_OUT_PROP: {"number": mfp_data["calories_out_exercise"]},
        NOTION_NET_CALS_PROP: {"number": mfp_data["net_calories"]},
        NOTION_PROTEIN_PROP: {"number": mfp_data["protein"]},
        NOTION_CARBS_PROP: {"number": mfp_data["carbs"]},
        NOTION_FATS_PROP: {"number": mfp_data["fats"]},
    }
    if "water_ml" in mfp_data and mfp_data["water_ml"] is not None : # Ensure water data is available
         properties[NOTION_WATER_PROP] = {"number": mfp_data["water_ml"]}


    page_data = {
        "parent": {"database_id": database_id},
        "properties": properties,
        # Optional: Add an icon if you like
        "icon": {"type": "emoji", "emoji": "🍎"}
    }
    try:
        notion_client.pages.create(**page_data)
        print(f"Successfully created Notion entry for {mfp_data['date']}.")
    except Exception as e:
        print(f"Error creating Notion entry for {mfp_data['date']}: {e}")
        print(f"Page data submitted: {page_data}")


def update_notion_entry(notion_client, page_id, mfp_data):
    """
    Updates an existing entry in the Notion database.
    Args:
        notion_client: The Notion client.
        page_id: The ID of the Notion page to update.
        mfp_data: A dictionary of MyFitnessPal data for the day.
    """
    properties = {
        NOTION_CALS_IN_PROP: {"number": mfp_data["calories_in"]},
        NOTION_CALS_OUT_PROP: {"number": mfp_data["calories_out_exercise"]},
        NOTION_NET_CALS_PROP: {"number": mfp_data["net_calories"]},
        NOTION_PROTEIN_PROP: {"number": mfp_data["protein"]},
        NOTION_CARBS_PROP: {"number": mfp_data["carbs"]},
        NOTION_FATS_PROP: {"number": mfp_data["fats"]},
    }
    if "water_ml" in mfp_data and mfp_data["water_ml"] is not None : # Ensure water data is available
         properties[NOTION_WATER_PROP] = {"number": mfp_data["water_ml"]}

    update_data = {
        "page_id": page_id,
        "properties": properties,
    }
    try:
        notion_client.pages.update(**update_data)
        print(f"Successfully updated Notion entry for {mfp_data['date']}.")
    except Exception as e:
        print(f"Error updating Notion entry for {mfp_data['date']} (Page ID: {page_id}): {e}")
        print(f"Update data submitted: {update_data}")

def main():
    # --- Check for required environment variables ---
    if not NOTION_TOKEN:
        print("Error: NOTION_TOKEN environment variable not set.")
        return
    if not NOTION_DATABASE_ID:
        print("Error: NOTION_MFP_DATABASE_ID environment variable not set.")
        return
    # MYFITNESSPAL_USERNAME is not strictly needed if cookies work

    print("Starting MyFitnessPal to Notion sync...")

    # Initialize MyFitnessPal client
    print("Initializing MyFitnessPal client...")
    try:
        mfp_client = get_mfp_client()
        # Test with a simple call to ensure client is working (optional)
        # This might fail if not logged in via browser / cookies not found
        # print(f"MyFitnessPal client initialized. Username (if available): {mfp_client.username}")
    except Exception as e:
        print(f"Failed to initialize MyFitnessPal client: {e}")
        print("Please ensure you are logged into MyFitnessPal in your web browser,")
        print("and that the `python-myfitnesspal` library can access its cookies.")
        print("You might need to install a specific browser cookie library like `pip install browser_cookie3`.")
        return

    # Initialize Notion client
    print("Initializing Notion client...")
    notion = Client(auth=NOTION_TOKEN)

    # --- Date Configuration ---
    # Sync data for today in the local timezone.
    # You can modify this to sync a range of dates or a specific date.
    today_local = datetime.datetime.now(local_tz).date()
    dates_to_sync = [today_local] # Sync today
    # Example: Sync last 7 days
    # dates_to_sync = [(today_local - datetime.timedelta(days=i)) for i in range(7)]


    print(f"Attempting to sync data for date(s): {[d.strftime('%Y-%m-%d') for d in dates_to_sync]}")

    for target_date in dates_to_sync:
        date_str = target_date.strftime('%Y-%m-%d')
        print(f"\n--- Processing: {date_str} ---")

        # 1. Get data from MyFitnessPal
        mfp_data = get_mfp_data_for_date(mfp_client, target_date)

        if not mfp_data:
            print(f"Skipping Notion update for {date_str} due to missing MFP data.")
            continue

        print(f"MyFitnessPal data for {date_str}: {mfp_data}")

        # 2. Check if entry exists in Notion
        print(f"Checking for existing Notion entry for {date_str}...")
        existing_page = entry_exists(notion, NOTION_DATABASE_ID, date_str)

        if existing_page:
            print(f"Found existing Notion page (ID: {existing_page['id']}) for {date_str}.")
            # 3a. If exists, check if update is needed
            if entry_needs_update(existing_page, mfp_data):
                print(f"Changes detected. Updating Notion entry for {date_str}...")
                update_notion_entry(notion, existing_page['id'], mfp_data)
            else:
                print(f"No changes detected. Notion entry for {date_str} is up to date.")
        else:
            # 3b. If not exists, create new entry
            print(f"No existing Notion entry found for {date_str}. Creating new one...")
            create_notion_entry(notion, NOTION_DATABASE_ID, mfp_data)

    print("\nMyFitnessPal to Notion sync process finished.")

if __name__ == '__main__':
    main()
