import  conf;
import  matplotlib.pyplot       as plt;
import  numpy                   as np;
import  os;
import  sys;
import  tensorflow              as tf;

# 注意：此專案的 Set2Set Loss 在 mixed_float16 精度下容易發生數值溢出 (NaN)。
# RTX 4080 使用預設的 float32 精度，訓練穩定度更高，不需要啟用 Mixed Precision。
# 如果未來想在 A100/H100 上啟用，請先確認 Loss Function 的數值穩定性。
# from tensorflow.keras import mixed_precision
# mixed_precision.set_global_policy('mixed_float16')

physical_devices = tf.config.list_physical_devices('GPU')
if physical_devices:
    try:
        # 動態分配 GPU 記憶體，不一次性佔滿
        for gpu in physical_devices:
            tf.config.experimental.set_memory_growth(gpu, True)
    except RuntimeError as e:
        print(e)

import  util;



def print_help():
    print("train.py {dataset}");
    exit(-1);
    

#
# Verify command line parameters
#

if len(sys.argv) != 2:
    print_help();
if not os.path.exists("datasets/" + sys.argv[1] + "/"):
    print("ERROR!!! Dataset '" + sys.argv[1] + "' not found.");
    print_help();

DATASET = sys.argv[1];


#
# Load and verify the dataset
#

FOLDER_NPY = "datasets/" + DATASET + "/npy/";

print("Loading dataset " + DATASET + "...");
print("  Training samples...");
xt = np.load(FOLDER_NPY + "xt.npy", allow_pickle=True).item();
print("  Validation samples...");
xv = np.load(FOLDER_NPY + "xv.npy", allow_pickle=True).item();

print("  Verifying sample shapes...");
first_user = list(xt.keys())[0];
first_sample_id = list(xt[first_user].keys())[0];
first_sample = xt[first_user][first_sample_id];
print("    Expected shape: " + str(first_sample.shape));

SEQUENCE_LENGTH = first_sample.shape[0];
INPUT_FEATURES = first_sample.shape[1];

for user, samples in xt.items():
    for sample_id, arr in samples.items():
        if arr.shape != first_sample.shape:
            print("ERROR!!! All samples must have the same shape (found " + str(arr.shape) + " in xt.npy).");

for user, samples in xv.items():
    for sample_id, arr in samples.items():
        if arr.shape != first_sample.shape:
            print("ERROR!!! All samples must have the same shape (found " + str(arr.shape) + " in xv.npy).");



#
# Initialize the training and validation generators
#

descriptor = {};
descriptor["SEQUENCE_LENGTH"] = SEQUENCE_LENGTH;
descriptor["INPUT_FEATURES"] = INPUT_FEATURES;

print("Initializing generators...");
print("  N (samples per user):        " + str(conf.N));
print("  K (sets per batch):          " + str(conf.K));
print("  Sample length (keystrokes):  " + str(SEQUENCE_LENGTH));
print("  Input features:              " + str(INPUT_FEATURES));

print("  Initializing training generator...");
import  training_generator;
ti, tg, tc = training_generator.get_generator(descriptor, xt);

print("  Initializing validation generator...");
import  validation_generator;
vi, vg, vc = validation_generator.get_generator(descriptor, xv);


#
# Initialize loss function
#

print("Initializing loss function...");
import  loss;
l = loss.get_loss();


#
# Compile the model
#

# util.clean_folder("model/"); # REMOVED: Do not clean folder to allow resuming

import  model;
descriptor = model.get_model_Type2Branch(descriptor);
m = descriptor["model"];
m.summary();

m.compile(
    optimizer=descriptor["optimizer"],
    loss=l,
);

import json
initial_epoch = 0
weights_path = "model/model.weights.h5"
state_path = "model/checkpoint_state.json"

if not os.path.exists("model/"):
    os.mkdir("model/")

es_wait = 0
es_best = None  # None 代表尚未初始化，on_train_begin 會處理

if os.path.exists(weights_path) and os.path.exists(state_path):
    print("==================================================")
    print("Found existing checkpoint! Loading weights to resume training...")
    try:
        m.load_weights(weights_path)
        with open(state_path, "r") as f:
            state = json.load(f)
            initial_epoch = state.get("epoch", 0)
            es_wait       = state.get("es_wait", 0)
            es_best       = state.get("es_best", None)
        print(f"Successfully loaded weights. Resuming from epoch {initial_epoch}")
        print(f"EarlyStopping state restored: wait={es_wait}, best={es_best}")
    except Exception as e:
        print("Error loading checkpoint:", e)
        print("Starting from epoch 0...")
        initial_epoch = 0
        es_wait       = 0
        es_best       = None
    print("==================================================")

#
# Initialize callbacks
#

class ResumableEarlyStopping(tf.keras.callbacks.EarlyStopping):
    """EarlyStopping 的擴充版：可在中斷後從 checkpoint 恢復 wait/best 狀態。"""

    def __init__(self, restored_wait=0, restored_best=None, **kwargs):
        super().__init__(**kwargs)
        self._restored_wait = restored_wait
        self._restored_best = restored_best

    def on_train_begin(self, logs=None):
        super().on_train_begin(logs)
        # 恢復上次中斷前的狀態
        if self._restored_best is not None:
            self.wait = self._restored_wait
            self.best = self._restored_best
            print(f"[EarlyStopping] 已恢復狀態：wait={self.wait}, best={self.best:.6f}")
        else:
            print(f"[EarlyStopping] 從頭開始計算 patience。")


class SaveModelFromEpochCallback(tf.keras.callbacks.ModelCheckpoint):
    def __init__(self, filepath, save_start_epoch=10, early_stopping_ref=None, **kwargs):
        super(SaveModelFromEpochCallback, self).__init__(filepath, **kwargs)
        self.save_start_epoch    = save_start_epoch
        self.early_stopping_ref  = early_stopping_ref

    def on_epoch_end(self, epoch, logs=None):
        if epoch >= self.save_start_epoch:
            super(SaveModelFromEpochCallback, self).on_epoch_end(epoch, logs)
            # 儲存 epoch 與 EarlyStopping 狀態，以便下次從正確的進度續練
            try:
                es_state = {}
                if self.early_stopping_ref is not None:
                    es_state["es_wait"] = int(self.early_stopping_ref.wait)
                    es_state["es_best"] = float(self.early_stopping_ref.best)
                with open("model/checkpoint_state.json", "w") as f:
                    json.dump({"epoch": epoch + 1, **es_state}, f, indent=2)
            except Exception as e:
                print("Failed to save checkpoint state:", e)


print("Initializing callbacks...");
es_callback = ResumableEarlyStopping(
    restored_wait = es_wait,
    restored_best = es_best,
    min_delta     = 0.0001,
    patience      = conf.EARLY_STOP_PATIENCE)

callbacks = [
    SaveModelFromEpochCallback(
        "model/model.weights.h5",
        save_start_epoch        = 1,
        early_stopping_ref      = es_callback,
        save_best_only          = True,
        save_weights_only       = True,
        initial_value_threshold = conf.EARLY_STOP_THRESHOLD,
        verbose                 = 2),
    es_callback,
];

if tc != None:
    callbacks.append(tc);
if vc != None:
    callbacks.append(vc);


#
# Train the model
#

print("Training model...");
history = descriptor["model"].fit(
    ti,
    validation_data     = vi,
    initial_epoch       = initial_epoch,
    epochs              = conf.EPOCHS,                    
    steps_per_epoch     = conf.TRAINING_STEPS,
    validation_steps    = conf.VALIDATION_STEPS,
    callbacks           = callbacks,
    verbose             = 2,
);



#
# Save the loss history
#

plt.plot(history.history['loss'])
plt.plot(history.history['val_loss'])
plt.title('model loss')
plt.ylabel('loss')
plt.xlabel('epoch')
plt.legend(['train', 'validate'], loc='upper left')
plt.savefig("LOSS.png");
